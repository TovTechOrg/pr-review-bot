"""doctor is read-only, degrades instead of erroring, and never leaks a value."""
from __future__ import annotations

import json
import subprocess

import pytest

import github_app
from config import settings
from scripts import deploy, doctor

SENTINEL = "SENTINEL-2b6d40af91ce7385-DO-NOT-LEAK"


@pytest.fixture(autouse=True)
def no_real_gh_calls(monkeypatch):
    """check_gh_auth shells out to the real `gh` CLI. Stubbed to "not
    authenticated" by default for every test in this file -- otherwise the
    suite's result would depend on whatever GitHub account (if any) happens
    to be authenticated on the machine actually running the tests, and
    `gh auth status` makes a real network call. Tests that exercise
    check_gh_auth's own behavior override this via monkeypatch."""
    monkeypatch.setattr(
        doctor, "_run_gh",
        lambda *args: subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=""),
    )


@pytest.fixture
def bare(monkeypatch):
    """A freshly-cloned checkout: nothing configured at all."""
    for field in (
        "github_app_id", "github_app_private_key", "github_webhook_secret",
        "database_url", "groq_api_key", "gemini_api_key",
        "vertex_gcp_service_account_key", "public_base_url", "render_api_key",
        "uptimerobot_api_key", "github_target_repo",
    ):
        monkeypatch.setattr(settings, field, type(getattr(settings, field))(), raising=False)


def test_llm_provider_row_requires_a_database(bare, monkeypatch):
    """provider/key_index are DB-only now, no env fallback (see
    docs/superpowers/specs/2026-09-09-provider-key-index-db-only-design.md)
    -- Step 5 (LLM provider) genuinely cannot clear before Step 4
    (Supabase/database) has, since there is no local-config-only state left
    to probe. deploy.check_provider SKIPs without DATABASE_URL, and that
    SKIP correctly makes llm_ready False."""
    monkeypatch.setattr(settings, "database_url", "")
    state, results = doctor.build_state("")
    by_name = {r.name: r for r in results}
    assert by_name["provider"].status == "SKIPPED"
    assert state.llm_ready is False


def test_a_bare_checkout_reports_step_two_not_a_crash(bare, monkeypatch):
    """Render not existing yet is the NORMAL state at the start, not a failure.

    Prereqs must PASS deterministically regardless of what tools happen to be
    installed on the machine running the suite, so PATH is monkeypatched the
    same way tests/test_prereqs.py does.
    """
    monkeypatch.setattr(doctor._prereqs.shutil, "which", lambda name: "/usr/bin/" + name)
    state, results = doctor.build_state("")
    step = doctor.current_step(state)
    assert step is not None
    assert step.number == 2, "prereqs pass in this environment; the App comes next"
    assert results, "a table is the deliverable even with nothing configured"


def test_no_check_raises_even_with_nothing_configured(bare):
    """Every row must render. _safe guarantees it; this pins that doctor uses it."""
    _state, results = doctor.build_state("")
    for result in results:
        assert result.status in ("PASS", "WARN", "FAIL", "SKIPPED")


def test_missing_operator_keys_fail_the_render_service_row(bare):
    _state, results = doctor.build_state("https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["render-service"].status == "FAIL"
    assert "RENDER_API_KEY" in by_name["render-service"].detail


def test_uptime_pinger_row_is_always_present(bare):
    names = {r.name for r in doctor.build_state("")[1]}
    assert "uptime-pinger" in names


def test_prereqs_row_names_the_install_hint_when_a_tool_is_missing(bare, monkeypatch):
    monkeypatch.setattr(doctor._prereqs.shutil, "which", lambda name: None)
    result = doctor.check_prereqs()
    assert result.status == "FAIL"
    assert "git-scm.com" in result.detail, "the hint's URL must reach the operator"


def test_local_config_row_reports_names_never_values(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SENTINEL, raising=False)
    result = doctor.check_local_config()
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert SENTINEL not in result.detail


def test_json_output_is_machine_readable_and_leak_free(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SENTINEL, raising=False)
    state, results = doctor.build_state("")
    payload = doctor.as_json(doctor.current_step(state), results)
    assert SENTINEL not in payload
    parsed = json.loads(payload)
    assert parsed["step"]["number"] >= 1
    assert parsed["step"]["command"]
    assert all({"name", "status", "detail"} <= set(c) for c in parsed["checks"])


def test_render_includes_the_you_are_here_line(bare):
    state, results = doctor.build_state("")
    text = doctor.render(doctor.current_step(state), results)
    assert "step" in text.lower()
    assert "of 8" in text


def test_render_reports_completion_when_every_step_is_satisfied():
    text = doctor.render(None, [deploy.CheckResult("config", "PASS")])
    assert "complete" in text.lower()


def test_render_completion_message_omits_the_caveat_when_nothing_fails():
    text = doctor.render(None, [deploy.CheckResult("config", "PASS")])
    assert "FAIL" not in text


def test_render_completion_message_notes_any_remaining_fail_row():
    """Step 8 being structurally unreachable used to let 'setup complete'
    print alongside a table with a FAIL row still showing -- self-
    contradictory. render() must call that out when it happens."""
    results = [
        deploy.CheckResult("config", "PASS"),
        deploy.CheckResult("uptime-pinger", "FAIL", "wrong URL"),
    ]
    text = doctor.render(None, results)
    assert "complete" in text.lower()
    assert "FAIL" in text
    assert "uptime-pinger" in text


def test_keepalive_is_false_when_uptime_pinger_fails(bare, monkeypatch):
    monkeypatch.setattr(deploy, "check_uptime_pinger", lambda base: deploy.CheckResult(
        "uptime-pinger", "FAIL", "points at the wrong URL"
    ))
    state, results = doctor.build_state("https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["uptime-pinger"].status == "FAIL"
    assert state.keepalive is False


def test_keepalive_is_true_when_uptime_pinger_passes(bare, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    monkeypatch.setattr(deploy, "check_uptime_pinger", lambda base: deploy.CheckResult(
        "uptime-pinger", "PASS", "interval 300s; status=2"
    ))
    state, results = doctor.build_state("https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["uptime-pinger"].status == "PASS"
    assert state.keepalive is True


def test_step_eight_is_reachable_when_public_url_clears_but_pinger_fails():
    """The bug this guards: step 6 and step 8 used to share one boolean, so
    the moment public_url/health cleared, step 8 cleared with it -- making it
    structurally unreachable as the reported current_step."""
    state = doctor.State(
        prereqs=True, app_credentials=True, app_installed=True, llm_ready=True,
        database=True, public_url=True, webhook=True, keepalive=False,
    )
    step = doctor.current_step(state)
    assert step is not None
    assert step.number == 8


def test_app_permissions_skips_without_app_credentials(bare):
    assert doctor.check_app_permissions().status == "SKIPPED"


def test_app_permissions_passes_when_it_matches_the_manifest_exactly(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)
    monkeypatch.setattr(
        github_app, "get_app_permissions",
        lambda: (dict(doctor.create_github_app.MANIFEST_PERMISSIONS),
                  list(doctor.create_github_app.MANIFEST_EVENTS)),
    )

    result = doctor.check_app_permissions()

    assert result.status == "PASS"


def test_app_permissions_fails_when_under_permissioned(bare, monkeypatch):
    """This is the check that makes creating the App by hand as safe as the
    automated manifest flow -- a hand-created App missing a permission the
    bot's code actually needs must FAIL, naming it, not just SKIP or PASS
    because credentials happen to be present."""
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)
    monkeypatch.setattr(
        github_app, "get_app_permissions", lambda: ({"contents": "read"}, ["pull_request"])
    )

    result = doctor.check_app_permissions()

    assert result.status == "FAIL"
    assert "issues" in result.detail


def test_app_permissions_warns_but_does_not_fail_when_over_permissioned(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)
    over_permissioned = dict(doctor.create_github_app.MANIFEST_PERMISSIONS, administration="write")
    monkeypatch.setattr(
        github_app, "get_app_permissions",
        lambda: (over_permissioned, list(doctor.create_github_app.MANIFEST_EVENTS)),
    )

    result = doctor.check_app_permissions()

    assert result.status == "WARN"
    assert "administration" in result.detail


def test_app_permissions_reports_a_lookup_failure_structurally(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(github_app, "get_app_permissions", _raise)

    result = doctor.check_app_permissions()

    assert result.status == "FAIL"
    assert "RuntimeError" in result.detail


def test_target_repo_covered_skips_when_target_repo_is_unset(bare):
    assert doctor.check_target_repo_covered().status == "SKIPPED"


def test_target_repo_covered_skips_when_target_repo_is_star(bare, monkeypatch):
    """"*" is a deliberate track-all choice, not an unconfigured one -- it
    still SKIPs (no specific repo to verify coverage for) but with a
    distinct message from the plain-unset case above."""
    monkeypatch.setattr(settings, "github_target_repo", "*")
    result = doctor.check_target_repo_covered()
    assert result.status == "SKIPPED"
    assert "track-all" in result.detail


def test_target_repo_covered_skips_without_app_credentials(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    result = doctor.check_target_repo_covered()
    assert result.status == "SKIPPED"
    assert "App credentials" in result.detail


def test_target_repo_covered_passes_when_installation_covers_it(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(github_app, "discover_installation_id_for_app", lambda: 42)
    monkeypatch.setattr(github_app, "list_installation_repos", lambda _id: ["owner/repo"])

    result = doctor.check_target_repo_covered()

    assert result.status == "PASS"
    assert "installation=42" in result.detail


def test_target_repo_covered_names_the_account_mismatch_as_a_possible_cause(bare, monkeypatch):
    """This is the check that closes the App-installation side of a
    browser-account vs. gh-account mismatch (see check_gh_auth for the other
    side) -- the detail must actually name that as a candidate cause, not
    just say 'not covered'."""
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(github_app, "discover_installation_id_for_app", lambda: 42)
    monkeypatch.setattr(github_app, "list_installation_repos", lambda _id: ["owner/other"])

    result = doctor.check_target_repo_covered()

    assert result.status == "FAIL"
    assert "owner/repo" in result.detail
    assert "different GitHub account" in result.detail


def test_target_repo_covered_reports_a_lookup_failure_structurally(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", "1", raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "x", raising=False)
    monkeypatch.setattr(settings, "github_webhook_secret", "x", raising=False)
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)

    result = doctor.check_target_repo_covered()

    assert result.status == "FAIL"
    assert "RuntimeError" in result.detail


def test_gh_auth_skips_when_gh_is_not_installed(bare, monkeypatch):
    monkeypatch.setattr(doctor._prereqs, "is_available", lambda _tool: False)
    result = doctor.check_gh_auth()
    assert result.status == "SKIPPED"


def test_gh_auth_fails_when_not_authenticated(bare, monkeypatch):
    """The autouse no_real_gh_calls fixture already stubs this to "not
    authenticated" -- this test just pins that gh-auth reports it as a FAIL
    naming the fix, rather than SKIPPED or a silent PASS."""
    monkeypatch.setattr(doctor._prereqs, "is_available", lambda _tool: True)
    result = doctor.check_gh_auth()
    assert result.status == "FAIL"
    assert "gh auth login" in result.detail


def test_gh_auth_passes_with_no_target_repo_once_authenticated(bare, monkeypatch):
    monkeypatch.setattr(doctor._prereqs, "is_available", lambda _tool: True)

    def _run_gh(*args):
        if args == ("auth", "status"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ("api", "user"):
            return subprocess.CompletedProcess(args, 0, "octocat\n", "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(doctor, "_run_gh", _run_gh)

    result = doctor.check_gh_auth()

    assert result.status == "PASS"
    assert "octocat" in result.detail


def test_gh_auth_passes_with_target_repo_star_once_authenticated(bare, monkeypatch):
    """"*" is a deliberate track-all choice, not an unconfigured one -- it
    still PASSes (no specific repo to check push access for) but with a
    distinct message from the plain-unset case above."""
    monkeypatch.setattr(settings, "github_target_repo", "*")
    monkeypatch.setattr(doctor._prereqs, "is_available", lambda _tool: True)

    def _run_gh(*args):
        if args == ("auth", "status"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ("api", "user"):
            return subprocess.CompletedProcess(args, 0, "octocat\n", "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(doctor, "_run_gh", _run_gh)

    result = doctor.check_gh_auth()

    assert result.status == "PASS"
    assert "octocat" in result.detail
    assert "track-all" in result.detail


def test_gh_auth_flags_a_repo_it_cannot_push_to(bare, monkeypatch):
    """This is the local-CLI side of the browser-account mismatch (see
    test_target_repo_covered_names_the_account_mismatch_as_a_possible_cause
    for the App-installation side) -- the detail must name the account
    mismatch as a candidate cause, not just relay gh's own permission text."""
    monkeypatch.setattr(doctor._prereqs, "is_available", lambda _tool: True)
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")

    def _run_gh(*args):
        if args == ("auth", "status"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ("api", "user"):
            return subprocess.CompletedProcess(args, 0, "octocat\n", "")
        if args[:3] == ("repo", "view", "owner/repo"):
            return subprocess.CompletedProcess(args, 0, "READ\n", "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(doctor, "_run_gh", _run_gh)

    result = doctor.check_gh_auth()

    assert result.status == "FAIL"
    assert "owner/repo" in result.detail
    assert "different GitHub account" in result.detail


def test_gh_auth_passes_when_it_can_push_to_the_target_repo(bare, monkeypatch):
    monkeypatch.setattr(doctor._prereqs, "is_available", lambda _tool: True)
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")

    def _run_gh(*args):
        if args == ("auth", "status"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ("api", "user"):
            return subprocess.CompletedProcess(args, 0, "octocat\n", "")
        if args[:3] == ("repo", "view", "owner/repo"):
            return subprocess.CompletedProcess(args, 0, "WRITE\n", "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(doctor, "_run_gh", _run_gh)

    result = doctor.check_gh_auth()

    assert result.status == "PASS"


def test_main_runs_the_full_wiring_and_prints_plain_text(capsys):
    """Exercises main()'s own wiring end-to-end -- deploy.resolve_base_url,
    build_state, current_step, render -- which no other test drives
    together. Runs against whatever local state exists; the point is the
    wiring succeeding and returning 0, not a specific check outcome."""
    result = doctor.main([])
    assert result == 0
    out = capsys.readouterr().out
    assert "step" in out.lower() or "complete" in out.lower()


def test_main_json_variant_prints_well_formed_json(capsys):
    result = doctor.main(["--json"])
    assert result == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "checks" in parsed


def test_doctor_never_calls_the_mutating_webhook_setter():
    """check_installation_and_webhook PATCHes the App's hook URL when wrong.
    doctor is read-only, so it must use get_webhook_url() instead."""
    import inspect

    source = inspect.getsource(doctor)
    assert "set_webhook_url" not in source
    assert "check_installation_and_webhook" not in source
