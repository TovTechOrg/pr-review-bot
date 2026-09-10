"""The consumer contract lag workflow -- structural checks only.

Asserted from the YAML rather than by running the workflow: schedule /
workflow_dispatch only register from a repository's default branch, so the
live trigger can't be exercised until this lands on main. See
docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md
section 6.2 and docs/superpowers/plans/2026-09-10-cross-repo-advisory-consumer-lag.md.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from scripts import check_consumer_contract as ccc

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "consumer-contract-lag.yml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML parses a bare `on:` key under YAML 1.1 as the boolean True, not
    # the string "on" -- .get("on", ...) alone is a KeyError waiting to
    # happen the moment someone edits this file.
    return workflow.get("on", workflow.get(True, {}))


def _steps() -> list[dict]:
    return _workflow()["jobs"]["consumer-contract-lag"]["steps"]


def _consumer_checkout_step() -> dict:
    return next(s for s in _steps() if s.get("id") == "consumer")


def test_the_advisory_workflow_is_not_attached_to_any_code_trigger():
    triggers = _triggers(_workflow())
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert "push" not in triggers
    assert "pull_request" not in triggers
    assert "workflow_run" not in triggers


def test_it_runs_daily_at_an_off_peak_minute():
    triggers = _triggers(_workflow())
    schedules = triggers["schedule"]
    assert len(schedules) == 1
    minute, hour, dom, month, dow = schedules[0]["cron"].split()
    assert dom == "*"
    assert month == "*"
    assert dow == "*"
    assert minute != "0"


def test_it_checks_out_the_consumer_repository_the_script_names():
    step = _consumer_checkout_step()
    assert step["with"]["repository"] == ccc.CONSUMER_REPO
    assert step["with"]["ref"] == "main"


def test_the_consumer_checkout_passes_no_token():
    step = _consumer_checkout_step()
    assert "token" not in step["with"]


def test_the_workflow_documents_what_happens_when_a_repo_goes_private():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "public" in text
    assert "private" in text


def test_the_consumer_checkout_tolerates_an_unreachable_sibling():
    step = _consumer_checkout_step()
    assert step["continue-on-error"] is True


def test_the_workflow_only_asks_for_read_permission():
    assert _workflow()["permissions"] == {"contents": "read"}


def test_the_compare_step_invokes_the_module_the_unit_tests_cover():
    commands = " ".join(step.get("run", "") for step in _steps())
    assert "scripts.check_consumer_contract" in commands
    consumer_path = _consumer_checkout_step()["with"]["path"]
    assert f"--consumer-root {consumer_path}" in commands


def test_the_compare_step_passes_the_consumer_checkout_s_own_recorded_outcome():
    """actions/checkout creates its target directory before it can fail on a
    private/renamed/deleted repository, so the checkout step's own outcome --
    not just whatever load_consumer finds on disk -- is what actually
    distinguishes an unreachable sibling from one that simply hasn't
    vendored a contract yet."""
    commands = " ".join(step.get("run", "") for step in _steps())
    consumer_step_id = _consumer_checkout_step()["id"]
    assert "--consumer-checkout-outcome" in commands
    assert f"steps.{consumer_step_id}.outcome" in commands


def test_the_blocking_workflow_is_untouched_and_still_names_no_sibling():
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "onboarding-wizard" not in text
    assert "workflow_run" not in text
