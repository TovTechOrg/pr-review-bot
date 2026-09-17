"""The weekly demo health check is ADVISORY. A red run means the deployed
demo is broken -- worth a look, never a push blocker. That must be
structural, not a convention: see .github/workflows/consumer-contract-lag.yml
for the same argument at length."""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "demo-health.yml"
)


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_it_can_never_gate_a_push_or_a_pull_request():
    """PyYAML parses a bare `on:` key as the BOOLEAN True, not the string
    "on" -- verified directly against this repo's existing workflows. Reading
    it as "on" silently yields None and makes this assertion vacuous."""
    workflow = _workflow()
    assert True in workflow, "expected a bare `on:` key"
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}


def test_it_runs_weekly():
    schedule = _workflow()[True]["schedule"]
    assert len(schedule) == 1
    # Day-of-week field pinned: a daily cron would wake the demo 7x a month
    # for no extra signal, against a shared 750-instance-hour budget.
    assert schedule[0]["cron"].split()[4] == "1"


def test_it_needs_no_write_scope_and_no_secret():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in _WORKFLOW.read_text(encoding="utf-8")


def test_it_actually_runs_the_check_script():
    steps = _workflow()["jobs"]["demo-health"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.demo_health_check" in commands
    assert "playwright install" in commands
