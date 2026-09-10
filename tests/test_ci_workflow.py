"""The drift job is what makes generated docs a guarantee rather than a habit.

Asserted structurally rather than by running CI: the job must exist, must
regenerate, and must fail on a diff.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_a_docs_job_exists_alongside_lint_and_test():
    jobs = _workflow()["jobs"]
    assert "docs" in jobs
    assert "lint-and-test" in jobs, "the existing job must not be replaced"


def test_the_docs_job_regenerates_and_fails_on_drift():
    steps = _workflow()["jobs"]["docs"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.gen_docs" in commands
    assert "git add -A" in commands
    assert "git diff --cached --exit-code" in commands
    assert "guide/reference" in commands


def test_the_docs_job_regenerates_before_diffing():
    """Order matters, not just presence: a diff-then-regenerate reordering
    would never observe drift, but a substring-only check on the joined
    commands (as above) can't tell the two orderings apart."""
    steps = _workflow()["jobs"]["docs"]["steps"]
    regenerate_index = next(
        i for i, step in enumerate(steps) if "scripts.gen_docs" in step.get("run", "")
    )
    diff_index = next(
        i for i, step in enumerate(steps) if "git diff --cached --exit-code" in step.get("run", "")
    )
    assert regenerate_index < diff_index


def test_the_docs_job_needs_no_database():
    """gen_docs reads class metadata and module constants only, so wiring a
    Postgres service to this job would be pure noise."""
    assert "services" not in _workflow()["jobs"]["docs"]


def test_a_pages_job_builds_and_deploys_the_guide():
    jobs = _workflow()["jobs"]
    assert "pages" in jobs
    commands = " ".join(s.get("run", "") for s in jobs["pages"]["steps"])
    assert "mkdocs build" in commands
    assert "--strict" in commands, "a broken internal link must fail the build"


def test_the_pages_job_has_the_permissions_deployment_needs():
    job = _workflow()["jobs"]["pages"]
    assert job["permissions"]["pages"] == "write"
    assert job["permissions"]["id-token"] == "write"


def test_pages_deploys_only_from_the_default_branch():
    """A Pages deploy from a feature branch would publish unreviewed docs."""
    job = _workflow()["jobs"]["pages"]
    assert "refs/heads/main" in job["if"]


def test_the_docs_job_regenerates_the_provisioning_contract_and_fails_on_drift():
    """Spec section 5.4: the same byte-compare that keeps guide/reference/
    honest, applied to the contract another repository vendors verbatim."""
    steps = _workflow()["jobs"]["docs"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.gen_contract" in commands
    assert "git add -A contracts/" in commands
    assert "git diff --cached --exit-code contracts/" in commands


def test_the_contract_is_regenerated_before_it_is_diffed():
    """Same reasoning as test_the_docs_job_regenerates_before_diffing: a
    diff-then-regenerate reordering would never observe drift, and a
    substring check on the joined commands cannot tell the orderings apart."""
    steps = _workflow()["jobs"]["docs"]["steps"]
    regenerate_index = next(
        i for i, step in enumerate(steps) if "scripts.gen_contract" in step.get("run", "")
    )
    diff_index = next(
        i
        for i, step in enumerate(steps)
        if "contracts/" in step.get("run", "")
        and "git diff --cached --exit-code" in step.get("run", "")
    )
    assert regenerate_index < diff_index


def test_the_contract_freshness_check_needs_no_database_or_sibling_checkout():
    """The bot's only blocking contract check is purely local: gen_contract
    reads class metadata and module constants, so no Postgres service -- and
    no sibling repository checkout, no pinned ref, no network (spec 5.4)."""
    job = _workflow()["jobs"]["docs"]
    assert "services" not in job
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "onboarding-wizard" not in text, (
        "the bot's blocking contract check must not depend on the consumer repo "
        "-- the advisory cross-repo job is a separate, later stage (spec 6.2)"
    )
