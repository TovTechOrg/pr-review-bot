"""Deterministic tests for scripts/deploy.py.

Two mocking harnesses are in play and they are not interchangeable:
`respx` intercepts `httpx` (Render, UptimeRobot, /healthz), while GitHub calls
go through PyGithub/`requests` and are therefore monkeypatched at the
`github_app` function boundary rather than at the HTTP layer. See
tests/test_github_app.py for why respx cannot see PyGithub traffic.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import psycopg
import pytest
import respx
import yaml

from config import OPERATIONAL_KEYS, Settings, settings
from providers import pricing
from scripts import _prereqs, deploy

BASE = "https://x.onrender.com"
HEALTH = f"{BASE}/healthz"


@pytest.fixture(autouse=True)
def _no_real_provider_credentials(monkeypatch):
    """deploy.py's tests never need a real provider key, and _wanted_env now
    reads every provider's credential -- so without this, a developer's .env
    flows into mocked request bodies and out through any respx match failure."""
    for name in ("gemini_api_key", "groq_api_key", "vertex_gcp_service_account_key"):
        monkeypatch.setattr(settings, name, "")


@pytest.fixture(autouse=True)
def _shipped_db_synced_defaults(monkeypatch):
    """Pin the 5 usage-cap/cooldown settings to their Settings class defaults,
    for the same reason _shipped_model_defaults exists: sync_env() now also
    writes these to runtime_config via sync_config_db(), so a locally-edited
    .env.config value could otherwise make a test non-deterministic, or trip
    sync_config_db()'s cooldown validity guard by accident."""
    for field in (
        "dispatcher_rereview_cooldown_seconds",
        "dispatcher_rereview_cooldown_max_seconds",
        "dispatcher_rereview_cooldown_factor",
        "key_usage_token_cap",
        "key_usage_reset_time_utc",
    ):
        monkeypatch.setattr(settings, field, Settings.model_fields[field].default)


@pytest.fixture(autouse=True)
def _runtime_config_schema_ok_by_default(request, monkeypatch):
    """sync_env() calls sync_config_db(), which now checks the live schema
    before writing -- so any test whose psycopg.connect is stubbed (most
    sync_env()/sync_config_db() tests, which never open a real DB) would
    otherwise have _missing_runtime_config_columns() read "table absent" off
    whatever canned fetchone() the stub returns, and refuse for a reason the
    test isn't about. Skipped for a test that requests the real `db` fixture
    (directly, or via _real_db_target/_runtime_config_missing_review_draft_prs)
    -- those want the genuine check running against the real test database."""
    if "db" in request.fixturenames:
        return
    monkeypatch.setattr(deploy, "_missing_runtime_config_columns", lambda: [])


@pytest.fixture(autouse=True)
def _shipped_model_defaults(monkeypatch):
    """Pin every provider's model var to its Settings class default, so these
    tests describe the SHIPPED configuration rather than whatever the
    developer's local .env.config happens to say. Same reason
    _no_real_provider_credentials exists: settings is a module-level singleton
    loaded from real env files, and deploy.py's pricing guards read every
    model var -- so a locally-edited model value would otherwise silently
    change what these tests assert."""
    for _credential, model_var in deploy._PROVIDERS.values():
        field = model_var.lower()
        monkeypatch.setattr(settings, field, Settings.model_fields[field].default)


def test_resolve_base_url_prefers_settings_and_strips_trailing_slash(monkeypatch):
    """A trailing slash would make check_uptime_pinger's exact-URL comparison
    fail against a correctly configured monitor (spec section 7.1)."""
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com/")
    assert deploy.resolve_base_url() == "https://x.onrender.com"


def test_resolve_base_url_falls_back_to_render_external_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://y.onrender.com/")
    assert deploy.resolve_base_url() == "https://y.onrender.com"


def test_resolve_base_url_empty_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.resolve_base_url() == ""


def test_render_report_aligns_columns_and_summarizes():
    report = deploy.render_report(
        [
            deploy.CheckResult("config", "PASS", ""),
            deploy.CheckResult("health", "FAIL", "HEAD /healthz -> 405 (GET ok)"),
            deploy.CheckResult("database", "SKIPPED", "set DATABASE_URL"),
        ]
    )
    lines = report.split("\n")
    # Status starts at the same column on every row.
    status_columns = {line.index(s) for line, s in zip(lines[:3], ["PASS", "FAIL", "SKIPPED"])}
    assert len(status_columns) == 1
    assert lines[-1] == f"1 failed, 1 skipped -- see {deploy._GUIDE_URL}"


def test_render_report_indents_continuation_lines():
    """A detail may wrap only to enumerate observed values; the continuation
    must align under the detail column, not the name column."""
    report = deploy.render_report(
        [
            deploy.CheckResult(
                "uptime-pinger", "FAIL", "no monitor matches /healthz\nfound: /healthz,"
            )
        ]
    )
    first, second = report.split("\n")[:2]
    assert second.startswith(" ")
    assert second.index("found:") == first.index("no monitor")


def test_render_report_summary_when_everything_passes():
    report = deploy.render_report([deploy.CheckResult("config", "PASS", "")])
    assert report.split("\n")[-1] == "all checks passed"


def test_a_warn_row_does_not_count_as_a_failure():
    report = deploy.render_report([
        deploy.CheckResult("config", "PASS"),
        deploy.CheckResult("pricing", "WARN", "GROQ_MODEL='x' has no pricing-table entry"),
    ])
    assert "1 warning" in report
    assert "failed" not in report


def test_report_as_json_carries_every_check_and_a_summary():
    results = [
        deploy.CheckResult("config", "PASS", ""),
        deploy.CheckResult("health", "FAIL", "HEAD /healthz -> 405 (GET ok)"),
        deploy.CheckResult("database", "SKIPPED", "set DATABASE_URL"),
    ]
    payload = deploy.report_as_json(results)
    assert payload["checks"] == [
        {"name": "config", "status": "PASS", "detail": ""},
        {"name": "health", "status": "FAIL", "detail": "HEAD /healthz -> 405 (GET ok)"},
        {"name": "database", "status": "SKIPPED", "detail": "set DATABASE_URL"},
    ]
    assert payload["summary"] == {"passed": 1, "warned": 0, "failed": 1, "skipped": 1}
    assert payload["guide_url"] == deploy._GUIDE_URL


def test_report_as_json_table_matches_render_report():
    results = [deploy.CheckResult("config", "PASS", "")]
    assert deploy.report_as_json(results)["table"] == deploy.render_report(results)


def test_report_as_json_is_actually_json_serializable():
    results = [deploy.CheckResult("uptime-pinger", "FAIL", "no monitor matches\nfound: none")]
    # round-trips without raising, and continuation lines survive as one string
    parsed = json.loads(json.dumps(deploy.report_as_json(results)))
    assert parsed["checks"][0]["detail"] == "no monitor matches\nfound: none"


@pytest.fixture
def complete_config(monkeypatch):
    """Every value check_config requires, present and valid."""
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key", "aGVsbG8=")
    monkeypatch.setattr(settings, "github_app_installation_id", 148449134)
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(settings, "dashboard_username", "dash-user")
    monkeypatch.setattr(settings, "dashboard_password", "dash-pass")
    monkeypatch.setattr(
        settings, "dashboard_session_secret", "dash-session-secret-value-padded-to-32-chars"
    )


def test_check_config_passes_when_everything_is_present(complete_config):
    assert deploy.check_config().status == "PASS"


def test_check_config_names_every_missing_key_at_once(complete_config, monkeypatch):
    """One run should surface all of them, not the first alphabetically."""
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "github_app_private_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail


def test_check_config_requires_dashboard_credentials(complete_config, monkeypatch):
    """check_config()'s own purpose is 'every setting the service needs is
    resolvable locally' -- main.py's lifespan now refuses to boot without
    these three, so this doctor check must name them too, or an operator
    would see a clean preflight report right before a crash-on-boot deploy."""
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")
    monkeypatch.setattr(settings, "dashboard_session_secret", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "DASHBOARD_USERNAME" in result.detail
    assert "DASHBOARD_PASSWORD" in result.detail
    assert "DASHBOARD_SESSION_SECRET" in result.detail


def test_check_config_rejects_a_dashboard_session_secret_shorter_than_32_chars(
    complete_config, monkeypatch
):
    """A short-but-non-empty secret passes the plain "is it set" check but is
    brute-forceable offline once a session cookie is captured -- main.py's
    lifespan enforces the same 32-char floor, and this doctor check must
    report it too, or an operator would see a clean preflight report right
    before a deploy that boots fine but signs weak session tokens."""
    monkeypatch.setattr(settings, "dashboard_session_secret", "short")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "DASHBOARD_SESSION_SECRET" in result.detail


def test_check_config_requires_the_installation_id(complete_config, monkeypatch):
    """GITHUB_APP_INSTALLATION_ID must always be configured explicitly, never
    guessed on the operator's behalf (ISSUES.md 2026-08-21) -- an unset (0)
    value is a missing required key, caught here before any API call."""
    monkeypatch.setattr(settings, "github_app_installation_id", 0)
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_INSTALLATION_ID" in result.detail


def test_check_config_requires_a_target_repo(complete_config, monkeypatch):
    """An empty GITHUB_TARGET_REPO used to be a valid "track all repos"
    value; it's now unconfigured -- "*" is the explicit spelling of
    track-all (2026-09-07), so an empty value is reported as missing."""
    monkeypatch.setattr(settings, "github_target_repo", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_TARGET_REPO" in result.detail


def test_check_config_passes_with_target_repo_star(complete_config, monkeypatch):
    """"*" is the explicit, non-empty "track all repos" value -- it must
    satisfy check_config() the same as a real allowlist entry."""
    monkeypatch.setattr(settings, "github_target_repo", "*")
    assert deploy.check_config().status == "PASS"


def test_providers_table_covers_every_supported_provider():
    """One table, read by check_provider, --sync-env and set_override.py, so a
    provider cannot be known to one consumer and unknown to another."""
    assert set(deploy._PROVIDERS) == {"gemini", "groq", "vertex"}
    for credential, model_var in deploy._PROVIDERS.values():
        assert credential and model_var


@pytest.mark.parametrize("model_var", ["GEMINI_MODEL", "GROQ_MODEL", "VERTEX_MODEL"])
def test_check_pricing_warns_on_an_unpriced_model(complete_config, monkeypatch, model_var):
    """A model with no pricing.py rate entry no longer crashes anything --
    estimate_cost_usd() returns None for it (spec section 6a) -- so this is a
    WARN, not a FAIL, reported for EVERY provider's var, not just the active
    one's (design spec 2026-08-18 section 6b)."""
    monkeypatch.setattr(settings, model_var.lower(), "totally-made-up-model")
    result = deploy.check_pricing()
    assert result.status == "WARN"
    assert model_var in result.detail
    assert "totally-made-up-model" in result.detail
    provider = next(p for p, (_c, mv) in deploy._PROVIDERS.items() if mv == model_var)
    assert provider in result.detail
    for known in pricing.models_for(provider):
        assert known in result.detail      # the fix is named, not just the fault
    # no DB override is active here -- the env var really is the source, so
    # the message must not claim an override is in play
    assert "override" not in result.detail.lower()


def test_unpriced_model_warns_and_does_not_fail_the_run(monkeypatch):
    """An unpriced model is a missing nice-to-have, not a blocker: the review
    still runs, it just carries no cost estimate (spec section 6b)."""
    monkeypatch.setattr(settings, "groq_model", "llama-3.1-8b-instant")
    result = deploy.check_pricing()
    assert result.status == "WARN"
    assert "llama-3.1-8b-instant" in result.detail
    assert "GROQ_MODEL" in result.detail


def test_pricing_check_passes_when_every_model_is_priced(monkeypatch):
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    assert deploy.check_pricing().status == "PASS"


def test_check_config_ignores_default_models(complete_config):
    """Regression guard against default/pricing-table drift: if a shipped
    model default ever stops being priced, a fresh clone would FAIL config out
    of the box with nothing edited."""
    for provider, (_credential, model_var) in deploy._PROVIDERS.items():
        default = Settings.model_fields[model_var.lower()].default
        assert pricing.is_known(provider, default), (
            f"{model_var}'s shipped default {default!r} has no {provider} pricing entry"
        )
    assert deploy.check_config().status == "PASS"


def test_check_config_and_check_pricing_each_report_their_own_problem(
    complete_config, monkeypatch
):
    """An unpriced model is check_pricing's problem to report, not
    check_config's (design spec 2026-08-18 section 6b) -- one run still
    surfaces every problem, just split across two rows instead of one."""
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "groq_model", "totally-made-up-model")
    config_detail = deploy.check_config().detail
    assert "GITHUB_WEBHOOK_SECRET" in config_detail
    assert "GROQ_MODEL" not in config_detail
    pricing_detail = deploy.check_pricing().detail
    assert "GROQ_MODEL" in pricing_detail


def test_check_pricing_uses_the_local_value_without_a_database_url(
    complete_config, monkeypatch
):
    """No DATABASE_URL -> no override to resolve -> local-only check, exactly
    the pre-existing behavior."""
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.check_pricing().status == "PASS"


def test_check_pricing_warns_on_an_unpriced_db_model_override(complete_config, monkeypatch):
    """The residual gap this fixes: set_override.py --model can put an
    unpriced model into live rotation, and check_pricing must not report PASS
    for it just because .env.config's own value is fine.

    The message must also correctly NAME the override as the source, not the
    env var -- VERTEX_MODEL may hold something else entirely and isn't even
    being read while this override is active, so blaming it would send an
    operator to edit the wrong thing. It must also carry the actual
    remediation command (set_override.py's real --clear-model --no-activate
    flags, not guessed syntax)."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(
        deploy, "_resolved_slot_models",
        lambda: {"gemini": None, "groq": None, "vertex": "totally-made-up-model"},
    )
    result = deploy.check_pricing()
    assert result.status == "WARN"
    assert "totally-made-up-model" in result.detail
    assert "vertex" in result.detail
    assert "override" in result.detail.lower()
    assert "VERTEX_MODEL is not consulted" in result.detail
    assert "scripts.set_override vertex --clear-model --no-activate" in result.detail


def test_check_pricing_passes_a_priced_db_model_override(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(
        deploy, "_resolved_slot_models",
        lambda: {"gemini": None, "groq": None, "vertex": "gemini-2.5-flash"},
    )
    assert deploy.check_pricing().status == "PASS"


def test_check_pricing_degrades_to_local_only_when_the_db_read_fails(
    complete_config, monkeypatch
):
    """A DB-read failure must degrade to the local-only check, never crash the
    whole pricing row for an unrelated reason -- mirrors check_provider()'s own
    degrade-on-exception behavior."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(deploy, "_resolved_slot_models", _boom)
    assert deploy.check_pricing().status == "PASS"


def test_check_pricing_reports_the_active_slots_model(
    _real_db_target, complete_config, monkeypatch, db_exec
):
    """_resolved_slot_models must resolve the model from the ACTIVE key-index
    slot's slot_config row, not the retired flat runtime_config.*_model
    columns (which nothing reads any more)."""
    monkeypatch.setattr(settings, "database_url", settings.database_url)
    db_exec(
        "INSERT INTO runtime_config (id, updated_at, groq_key_index) VALUES (1, %s, 1) "
        "ON CONFLICT (id) DO UPDATE SET groq_key_index = 1",
        ("2026-09-08T00:00:00+00:00",),
    )
    db_exec(
        "INSERT INTO slot_config (provider, slot_index, model, updated_at) "
        "VALUES ('groq', 1, 'totally-made-up-model', %s)",
        ("2026-09-08T00:00:00+00:00",),
    )
    result = deploy.check_pricing()
    assert result.status == "WARN"
    assert "totally-made-up-model" in result.detail
    assert "groq" in result.detail


def test_check_pricing_warns_when_the_active_slot_has_no_model(
    _real_db_target, complete_config, monkeypatch, db_exec
):
    db_exec(
        "INSERT INTO runtime_config (id, updated_at, groq_key_index) VALUES (1, %s, 2) "
        "ON CONFLICT (id) DO UPDATE SET groq_key_index = 2",
        ("2026-09-08T00:00:00+00:00",),
    )
    # No slot_config row for (groq, 2) -- falls back to the local env value,
    # which complete_config already primes with a priced model.
    assert deploy.check_pricing().status == "PASS"


def test_check_config_never_prints_a_secret_value(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET_VALUE")
    result = deploy.check_config()
    assert "gsk_SUPER_SECRET_VALUE" not in result.detail


def test_check_config_reports_a_missing_private_key_as_missing(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail


def test_boot_credentials_live_fails_without_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"
    assert "RENDER_API_KEY" in result.detail


def test_boot_credentials_live_fails_when_no_service_found(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list(name="something-else"))
        )
        result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"
    assert "no service named" in result.detail


def test_boot_credentials_live_passes_when_all_present(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_INSTALLATION_ID": "155887152",
                        "GITHUB_APP_PRIVATE_KEY": "aGVsbG8=",
                        "GITHUB_WEBHOOK_SECRET": "s3cret",
                        "DATABASE_URL": "postgresql://u:p@h/db",
                        "DASHBOARD_USERNAME": "dash-user",
                        "DASHBOARD_PASSWORD": "dash-pass",
                        "DASHBOARD_SESSION_SECRET": "dash-session-secret",
                    }
                ),
            )
        )
        result = deploy.check_boot_credentials_live()
    assert result.status == "PASS"


def test_boot_credentials_live_fails_naming_exactly_the_missing_ones(monkeypatch):
    """The exact failure mode this check exists to catch: a renamed
    boot-critical var (e.g. GITHUB_APP_PRIVATE_KEY_B64 -> GITHUB_APP_PRIVATE_KEY)
    left the new name empty on Render while the old one lingered unused."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_INSTALLATION_ID": "155887152",
                        "GITHUB_WEBHOOK_SECRET": "s3cret",
                        "DATABASE_URL": "postgresql://u:p@h/db",
                    }
                ),
            )
        )
        result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail
    assert "GITHUB_APP_ID" not in result.detail


def test_boot_credentials_live_also_requires_installation_id(monkeypatch):
    """Regression: this check used to list only four vars, even though
    main.py's lifespan also refuses to boot without GITHUB_APP_
    INSTALLATION_ID (ISSUES.md 2026-08-21 made that unconditional, not just
    a discovery fallback). A live Render service missing it would crash-loop
    while this check still reported PASS. (LLM_PROVIDER is no longer in this
    list at all -- provider is a runtime_config row now, not a Render env
    var; see docs/superpowers/specs/2026-09-09-provider-key-index-db-only-
    design.md.)"""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_PRIVATE_KEY": "aGVsbG8=",
                        "GITHUB_WEBHOOK_SECRET": "s3cret",
                        "DATABASE_URL": "postgresql://u:p@h/db",
                    }
                ),
            )
        )
        result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"
    assert "GITHUB_APP_INSTALLATION_ID" in result.detail
    assert "LLM_PROVIDER" not in result.detail


def test_boot_credentials_live_also_requires_dashboard_credentials(monkeypatch):
    """Same regression shape as the installation-id case above:
    main.py's lifespan also refuses to boot without the three DASHBOARD_*
    vars now, so a live Render service missing any of them would crash-loop
    while this check still reported PASS if it didn't know about them."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_INSTALLATION_ID": "155887152",
                        "GITHUB_APP_PRIVATE_KEY": "aGVsbG8=",
                        "GITHUB_WEBHOOK_SECRET": "s3cret",
                        "DATABASE_URL": "postgresql://u:p@h/db",
                    }
                ),
            )
        )
        result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"
    assert "DASHBOARD_USERNAME" in result.detail
    assert "DASHBOARD_PASSWORD" in result.detail
    assert "DASHBOARD_SESSION_SECRET" in result.detail


def test_boot_credentials_live_never_leaks_a_fetched_value(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_INSTALLATION_ID": "155887152",
                        "GITHUB_APP_PRIVATE_KEY": "SUPER_SECRET_PEM_B64",
                        "GITHUB_WEBHOOK_SECRET": "SUPER_SECRET_WEBHOOK",
                        "DATABASE_URL": "postgresql://u:SUPER_SECRET_PW@h/db",
                    }
                ),
            )
        )
        result = deploy.check_boot_credentials_live()
    assert "SUPER_SECRET" not in result.detail


def test_boot_credentials_live_fails_on_render_api_error(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(500))
        result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"


@pytest.fixture
def github_seam(monkeypatch):
    """Monkeypatch the github_app boundary and record webhook writes.

    The check's job is the decision logic (read -> compare -> conditionally
    write); github_app's own HTTP behavior is covered in tests/test_github_app.py
    with the requests-level fake_transport harness, which respx cannot replace.

    `list_installation_repos` records the installation_id it was called with
    in state["list_repos_called_with"] -- a regression guard for the bug
    where check_installation_and_webhook used to call it with no argument
    (reading a stale/unset settings value internally) instead of passing
    through the id it just discovered.
    """
    import github_app

    state = {
        "installation_id": 424242,
        "current_url": "",
        "written": [],
        "repos": ["owner/repo"],
        "list_repos_called_with": None,
    }

    def _list_installation_repos(installation_id):
        state["list_repos_called_with"] = installation_id
        return state["repos"]

    monkeypatch.setattr(
        github_app, "discover_installation_id_for_app", lambda: state["installation_id"]
    )
    monkeypatch.setattr(github_app, "list_installation_repos", _list_installation_repos)
    monkeypatch.setattr(github_app, "get_webhook_url", lambda: state["current_url"])
    monkeypatch.setattr(github_app, "set_webhook_url", lambda url: state["written"].append(url))
    # GITHUB_APP_INSTALLATION_ID is required and verified against discovery
    # (ISSUES.md 2026-08-21) -- pinned to match state["installation_id"] by
    # default so every test not specifically exercising that verification
    # isn't tripped by an unrelated mismatch/unset FAIL.
    monkeypatch.setattr(settings, "github_app_installation_id", state["installation_id"])
    return state


def test_webhook_already_correct_passes_without_writing(github_seam):
    github_seam["current_url"] = "https://x.onrender.com/webhook"
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "PASS"
    assert "already correct" in result.detail
    assert github_seam["written"] == []          # no PATCH issued


def test_webhook_mismatch_is_updated(github_seam):
    github_seam["current_url"] = "https://old.example/webhook"
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "PASS"
    assert github_seam["written"] == ["https://x.onrender.com/webhook"]
    assert "https://old.example/webhook" in result.detail


def test_webhook_absent_is_set_on_first_deploy(github_seam):
    github_seam["current_url"] = ""
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "PASS"
    assert github_seam["written"] == ["https://x.onrender.com/webhook"]


def test_app_not_installed_fails_with_an_actionable_detail(github_seam, monkeypatch):
    import github_app

    def _raise():
        raise github_app.AppNotInstalledError("not installed")

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "install" in result.detail.lower()
    assert github_seam["written"] == []


def test_failed_webhook_read_does_not_write(github_seam, monkeypatch):
    """Writing blind after a failed read is how a correct URL gets clobbered."""
    from github import GithubException

    import github_app

    def _raise():
        raise GithubException(500, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "get_webhook_url", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "500" in result.detail
    assert github_seam["written"] == []


def test_failed_webhook_write_fails_with_the_status(github_seam, monkeypatch):
    """A failing PATCH must render an actionable, status-bearing FAIL like the
    read path above it -- not fall through to the generic _safe() catch-all."""
    from github import GithubException

    import github_app

    github_seam["current_url"] = "https://old.example/webhook"

    def _raise(url):
        raise GithubException(502, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "set_webhook_url", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "502" in result.detail
    assert "webhook write failed" in result.detail


def test_installation_lookup_non_404_reports_the_underlying_status(github_seam, monkeypatch):
    """A 401 (bad key) and a 502 (GitHub degraded) must render differently --
    the generic RuntimeError message alone collapses both to the same string."""
    from github import GithubException

    import github_app

    def _raise():
        try:
            raise GithubException(401, {"message": "bad credentials"}, None)
        except GithubException as exc:
            raise RuntimeError("installation lookup failed") from exc

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "401" in result.detail
    assert github_seam["written"] == []


def test_multiple_installations_error_renders_the_actionable_message(github_seam, monkeypatch):
    """The 'multiple installations' RuntimeError has no GithubException cause
    (it's raised directly, not chained) -- check_installation_and_webhook's
    else branch must surface its actual message (naming the ambiguous
    accounts) rather than falling back to the generic
    'installation lookup failed; check App ID / private key' text, which
    would misdiagnose an ambiguous-installation state as a credentials
    problem."""
    import github_app

    def _raise():
        raise RuntimeError(
            "GitHub App has multiple installations (org-a, org-b) -- set "
            "GITHUB_APP_INSTALLATION_ID explicitly to pick one."
        )

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "org-a" in result.detail
    assert "org-b" in result.detail
    assert "installation lookup failed" not in result.detail


def test_installation_and_webhook_track_all_reports_installed_repo_count(github_seam):
    github_seam["repos"] = ["owner/a", "owner/b", "owner/c"]
    github_seam["current_url"] = "https://x.onrender.com/webhook"
    result = deploy.check_installation_and_webhook(frozenset(), "https://x.onrender.com")
    assert result.status == "PASS"
    assert "tracking all 3" in result.detail


def test_installation_and_webhook_flags_allowlist_entry_not_covered(github_seam):
    github_seam["repos"] = ["owner/repo"]
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo", "owner/missing-repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "owner/missing-repo" in result.detail


def test_installation_and_webhook_names_both_possible_causes_of_a_missing_repo(github_seam):
    """ISSUES.md 2026-08-17: a missing allowlist entry could be a typo (never
    installed) or a config-hygiene nit (installed, later removed) -- GitHub's
    API can't tell them apart, so the detail must name both rather than
    implying only one."""
    github_seam["repos"] = []
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/missing-repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "typo" in result.detail
    assert "removed" in result.detail


def test_installation_and_webhook_matches_allowlist_case_insensitively(github_seam):
    """GitHub repo names are case-insensitive; an allowlist entry need not
    match the installation's reported casing exactly."""
    github_seam["repos"] = ["owner/repo"]
    result = deploy.check_installation_and_webhook(
        frozenset({"Owner/Repo"}), "https://x.onrender.com"
    )
    assert result.status == "PASS"


def test_installation_and_webhook_passes_the_discovered_id_to_list_installation_repos(
    github_seam, monkeypatch,
):
    """Regression guard: list_installation_repos must be called with the id
    discover_installation_id_for_app() just returned, not read internally
    from settings (that was the bug -- see github_app.py's
    list_installation_repos docstring)."""
    github_seam["installation_id"] = 777777
    monkeypatch.setattr(settings, "github_app_installation_id", 777777)
    deploy.check_installation_and_webhook(frozenset(), "https://x.onrender.com")
    assert github_seam["list_repos_called_with"] == 777777


def test_installation_and_webhook_fails_when_installation_id_is_unset(github_seam, monkeypatch):
    monkeypatch.setattr(settings, "github_app_installation_id", 0)
    result = deploy.check_installation_and_webhook(frozenset(), "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "GITHUB_APP_INSTALLATION_ID" in result.detail


def test_installation_and_webhook_fails_when_pinned_id_does_not_match_discovery(
    github_seam, monkeypatch
):
    """A pinned GITHUB_APP_INSTALLATION_ID that no longer matches the App's
    actual installation (e.g. uninstalled and reinstalled) must fail this
    check, not silently use the discovered id instead."""
    monkeypatch.setattr(settings, "github_app_installation_id", 111111)
    result = deploy.check_installation_and_webhook(frozenset(), "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "111111" in result.detail
    assert str(github_seam["installation_id"]) in result.detail
    assert github_seam["written"] == []


def test_installation_and_webhook_repo_list_failure_reports_status(github_seam, monkeypatch):
    from github import GithubException

    import github_app

    def _raise(installation_id):
        raise GithubException(500, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "list_installation_repos", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "500" in result.detail


def test_health_passes_when_get_and_head_both_return_200():
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "PASS"


def test_health_fails_when_head_is_405_even_though_get_is_200():
    """The exact regression that silently broke keep-warm for 71 minutes."""
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(405))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
    assert "HEAD" in result.detail


def test_health_fails_when_get_is_not_200():
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(503))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
    assert "503" in result.detail


def test_health_fails_on_a_transport_error():
    with respx.mock:
        respx.get(HEALTH).mock(side_effect=httpx.ConnectError("refused"))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"


DEAD_DB_URL = "postgresql://u:pw@127.0.0.1:1/postgres?connect_timeout=1"

# A stand-in for "some database is configured" in sync_env() tests that are
# about something else entirely (model overrides) and stub every DB accessor,
# so this URL is never dialed. It must NOT be a localhost/127.0.0.1/.internal
# host: sync_env()'s local-test-database guard refuses those outright and
# would short-circuit the guard each such test is actually named for.
REMOTE_PLACEHOLDER_DB_URL = "postgresql://u:p@db.example.com/x"


def test_database_skips_with_a_hint_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    result = deploy.check_database()
    assert result.status == "SKIPPED"
    assert "DATABASE_URL" in result.detail


def test_database_fails_fast_when_unreachable(monkeypatch):
    """127.0.0.1:1 refuses immediately, so this costs about a second."""
    monkeypatch.setattr(settings, "database_url", DEAD_DB_URL)
    result = deploy.check_database()
    assert result.status == "FAIL"
    assert "connect" in result.detail.lower()


def test_database_passes_against_the_provisioned_test_database(db, db_url, monkeypatch):
    """The `db` fixture opens the pool and creates the schema, so `tickets` exists."""
    monkeypatch.setattr(settings, "database_url", db_url)
    result = deploy.check_database()
    assert result.status == "PASS"
    assert "tickets present" in result.detail


def test_database_distinguishes_reachable_but_unprovisioned(db, db_url, db_exec, monkeypatch):
    """A DATABASE_URL pointing at the wrong Supabase project answers SELECT 1
    but has no tickets table -- a setup mistake a bare SELECT 1 calls success."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS tickets")
    result = deploy.check_database()
    assert result.status == "FAIL"
    assert "tickets" in result.detail


SENTINEL_PASSWORD = "sentinel-pw-must-not-appear"


def test_check_database_failure_never_leaks_the_connection_string(monkeypatch):
    """CLAUDE.md: no secret is ever logged. database_url carries the password,
    so the failure detail must describe the failure shape -- exception type and
    the non-secret hostname -- and never interpolate the URL."""
    monkeypatch.setattr(
        settings,
        "database_url",
        f"postgresql://someuser:{SENTINEL_PASSWORD}@127.0.0.1:1/postgres?connect_timeout=1",
    )
    result = deploy.check_database()
    assert result.status == "FAIL"
    rendered = result.detail + repr(result) + deploy.render_report([result])
    assert SENTINEL_PASSWORD not in rendered


def test_runtime_config_schema_skips_with_a_hint_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    result = deploy.check_runtime_config_schema()
    assert result.status == "SKIPPED"
    assert "DATABASE_URL" in result.detail


def test_runtime_config_schema_passes_against_the_provisioned_test_database(
    db, db_url, monkeypatch
):
    monkeypatch.setattr(settings, "database_url", db_url)
    result = deploy.check_runtime_config_schema()
    assert result.status == "PASS"


@pytest.fixture
def _runtime_config_missing_review_draft_prs(db, db_exec):
    """Drops review_draft_prs to reproduce the real gap (CREATE TABLE IF NOT
    EXISTS can't backfill a column into an already-provisioned table), then
    restores it. The test database's schema is session-scoped and persists
    across tests (only its rows are truncated between them -- see the `db`
    fixture), so a test that breaks the schema must repair it itself or every
    later test in the session inherits a mutant runtime_config table."""
    db_exec("ALTER TABLE runtime_config DROP COLUMN review_draft_prs")
    yield
    db_exec("ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS review_draft_prs BOOLEAN")


def test_runtime_config_schema_fails_naming_a_missing_column_and_the_fix(
    _runtime_config_missing_review_draft_prs, db_url, monkeypatch
):
    """review_draft_prs shipped in review_queue/store.py's _SCHEMA after the live
    database's runtime_config table already existed -- CREATE TABLE IF NOT
    EXISTS is a no-op against it, so this reproduces exactly the gap that
    left review_draft_prs missing on the real Render database (the bug this
    check exists to catch before --sync-config-db hits a raw UndefinedColumn)."""
    monkeypatch.setattr(settings, "database_url", db_url)
    result = deploy.check_runtime_config_schema()
    assert result.status == "FAIL"
    assert "review_draft_prs" in result.detail
    assert (
        "ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS review_draft_prs BOOLEAN"
        in result.detail
    )


def test_runtime_config_schema_reports_an_absent_table_distinctly(
    db, db_url, db_exec, monkeypatch
):
    """Missing entirely (never booted on this DB) is a different fix -- deploy
    once so the boot DDL provisions it -- than a column missing from an
    already-provisioned table, which needs a manual ALTER. Conflating the two
    would recommend 15 ALTER statements against a table that doesn't exist."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE runtime_config")
    result = deploy.check_runtime_config_schema()
    assert result.status == "FAIL"
    assert "does not exist" in result.detail
    assert "ALTER TABLE" not in result.detail


@pytest.fixture
def _runtime_config_shadowed_by_another_schema(db, db_exec):
    """A same-named `runtime_config` table in a non-public schema, WITH the
    column the public one is missing. Regression fixture for the
    table_schema gap: the live-columns query used to filter on table_name
    alone, so this other schema's columns could mask a genuinely missing
    public column. Drops the shadow schema afterward; the public table's
    review_draft_prs column is restored by the caller's own
    _runtime_config_missing_review_draft_prs fixture."""
    db_exec("CREATE SCHEMA IF NOT EXISTS shadow")
    db_exec("CREATE TABLE shadow.runtime_config (review_draft_prs BOOLEAN)")
    yield
    db_exec("DROP SCHEMA shadow CASCADE")


def test_runtime_config_schema_ignores_a_same_named_table_in_another_schema(
    _runtime_config_missing_review_draft_prs,
    _runtime_config_shadowed_by_another_schema,
    db_url,
    monkeypatch,
):
    """review_draft_prs is missing from public.runtime_config but present on
    shadow.runtime_config -- the check must still report it missing (schema-
    qualified), not be masked by the other schema's column of the same name."""
    monkeypatch.setattr(settings, "database_url", db_url)
    result = deploy.check_runtime_config_schema()
    assert result.status == "FAIL"
    assert "review_draft_prs" in result.detail


RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(name="pr-review-engine", service_id="srv-1"):
    return [{"service": {"id": service_id, "name": name}}]


def _deploy_list(status):
    return [{"deploy": {"id": "dep-1", "status": status}}]


def test_render_service_fails_with_a_hint_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "RENDER_API_KEY" in result.detail


def test_render_service_passes_when_latest_deploy_is_live(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=_deploy_list("live"))
        )
        result = deploy.check_render_service()
    assert result.status == "PASS"


def test_render_service_fails_and_names_a_non_live_status(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=_deploy_list("build_failed"))
        )
        result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "build_failed" in result.detail


def test_render_service_fails_when_the_configured_name_is_absent(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list(name="something-else"))
        )
        result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "pr-review-engine" in result.detail


def test_render_service_never_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_SUPER_SECRET")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(401, json={}))
        result = deploy.check_render_service()
    assert "rnd_SUPER_SECRET" not in result.detail


def _deploys_response(deploy_obj):
    return httpx.Response(200, json=[{"deploy": deploy_obj}])


@respx.mock
def test_render_service_reports_the_live_commit(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cdaffffffff"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "4e39cda" in result.detail


@respx.mock
def test_render_service_fails_when_local_head_is_not_deployed(monkeypatch):
    """The never-push operator's trap: changes that never reached the build."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("1b10b18", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "4e39cda" in result.detail and "1b10b18" in result.detail


@respx.mock
def test_render_service_fails_on_a_dirty_working_tree(monkeypatch):
    """Uncommitted changes can be in no build, by construction."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", True))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "dirty" in result.detail


@respx.mock
def test_render_service_reports_an_image_without_claiming_verification(monkeypatch):
    """No local comparison is possible, and the row must not imply one."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "image": {"ref": "ghcr.io/you/pr-review:v3"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "ghcr.io/you/pr-review:v3" in result.detail
    assert "no local comparison" in result.detail


@respx.mock
def test_render_service_degrades_when_render_reports_no_artifact(monkeypatch):
    """Assumption 4 is unverified: a missing field must never produce a FAIL."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response({"id": "dep-abc", "status": "live"})
    )
    assert deploy.check_render_service().status == "PASS"


@respx.mock
def test_render_service_skips_the_comparison_outside_a_git_repo(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: None)
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "no git" in result.detail


UPTIMEROBOT = "https://api.uptimerobot.com/v2/getMonitors"


def _monitors(*monitors):
    return {"stat": "ok", "monitors": list(monitors)}


def test_uptime_pinger_fails_with_a_hint_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "")
    result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "UPTIMEROBOT_API_KEY" in result.detail


def test_uptime_pinger_passes_for_an_active_five_minute_monitor(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH, "status": 2, "interval": 300})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "PASS"


def test_uptime_pinger_fails_on_a_near_miss_url(monkeypatch):
    """The real outage: a trailing comma, firing on schedule, 404ing every time."""
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH + ",", "status": 2, "interval": 300})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert HEALTH + "," in result.detail       # the near-miss is visible on sight


def test_uptime_pinger_fails_when_paused(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH, "status": 0, "interval": 300})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "paused" in result.detail


def test_uptime_pinger_fails_when_the_interval_lets_the_instance_sleep(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH, "status": 2, "interval": 1800})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "1800" in result.detail


def test_uptime_pinger_never_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_SUPER_SECRET")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(return_value=httpx.Response(500, json={}))
        result = deploy.check_uptime_pinger(BASE)
    assert "u_SUPER_SECRET" not in result.detail


def _stub_all_checks(monkeypatch, statuses):
    """Replace all twelve checks with constant results, in report order."""
    names = [
        "config", "pricing", "boot-creds-live", "github-app", "health", "database",
        "runtime-config", "provider", "provider-live", "api-key-live",
        "render-service", "uptime-pinger",
    ]
    fns = [
        "check_config",
        "check_pricing",
        "check_boot_credentials_live",
        "check_installation_and_webhook",
        "check_health_endpoint",
        "check_database",
        "check_runtime_config_schema",
        "check_provider",
        "check_provider_live",
        "check_api_key_live",
        "check_render_service",
        "check_uptime_pinger",
    ]
    for fn, name, status in zip(fns, names, statuses):
        monkeypatch.setattr(
            deploy, fn, (lambda n, s: lambda *args: deploy.CheckResult(n, s, ""))(name, status)
        )


@pytest.fixture
def runnable(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", BASE)


def test_main_returns_zero_when_all_pass_or_skip(runnable, monkeypatch, capsys):
    _stub_all_checks(monkeypatch, ["PASS"] * 8 + ["SKIPPED"] * 4)
    assert deploy.main([]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_returns_one_when_any_check_fails(runnable, monkeypatch, capsys):
    _stub_all_checks(
        monkeypatch,
        ["PASS", "PASS", "PASS", "FAIL", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS",
         "SKIPPED", "SKIPPED"],
    )
    assert deploy.main([]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_main_with_sync_env_falls_through_to_the_checklist_on_success(
    runnable, monkeypatch, capsys
):
    """Spec section 8 step 7: a successful sync must not skip the post-sync
    checklist -- it is the thing that proves the sync actually took."""
    _stub_all_checks(monkeypatch, ["PASS"] * 8 + ["SKIPPED"] * 4)
    monkeypatch.setattr(deploy, "sync_env", lambda: 0)
    assert deploy.main(["--sync-env"]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_with_sync_env_returns_early_without_the_checklist_on_failure(
    runnable, monkeypatch, capsys
):
    """A non-zero sync_env() must short-circuit main() before run_checks/
    render_report ever run -- printing the table after a failed sync would
    misleadingly suggest the sync itself is fine."""
    _stub_all_checks(monkeypatch, ["PASS"] * 8 + ["SKIPPED"] * 4)
    monkeypatch.setattr(deploy, "sync_env", lambda: 2)
    assert deploy.main(["--sync-env"]) == 2
    assert "all checks passed" not in capsys.readouterr().out


def test_main_proceeds_with_a_target_repo_of_star_track_all_mode(monkeypatch, capsys):
    """GITHUB_TARGET_REPO=* (track-all mode) is a real, non-empty value like
    any other -- it must not block main() the way a missing base URL does.
    (An empty value would instead surface as a check_config FAIL, not an
    early exit -- checks are stubbed here so that's not what's under test.)"""
    monkeypatch.setattr(settings, "github_target_repo", "*")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    _stub_all_checks(monkeypatch, ["PASS"] * 8 + ["SKIPPED"] * 4)
    assert deploy.main([]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_returns_two_without_a_base_url(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.main([]) == 2


def test_main_health_only_passes_when_healthy(monkeypatch, capsys):
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        assert deploy.main(["--health-only"]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_health_only_fails_when_unhealthy(monkeypatch, capsys):
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(500))
        respx.head(HEALTH).mock(return_value=httpx.Response(500))
        assert deploy.main(["--health-only"]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_main_health_only_json_emits_a_parseable_check(monkeypatch, capsys):
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(500))
        respx.head(HEALTH).mock(return_value=httpx.Response(500))
        assert deploy.main(["--health-only", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"] == [
        {"name": "health", "status": "FAIL", "detail": "GET -> 500, HEAD -> 500"}
    ]
    assert payload["summary"]["failed"] == 1


def test_main_json_emits_the_full_checklist_as_one_object(runnable, monkeypatch, capsys):
    _stub_all_checks(monkeypatch, ["PASS"] * 8 + ["SKIPPED"] * 4)
    assert deploy.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in payload["checks"]] == [spec.name for spec in deploy.CHECKS]
    assert payload["summary"] == {"passed": 8, "warned": 0, "failed": 0, "skipped": 4}
    assert "all checks passed" in payload["table"]


def test_main_health_only_works_without_a_target_repo(monkeypatch):
    """The whole point of --health-only: no GITHUB_TARGET_REPO needed."""
    monkeypatch.setattr(settings, "github_target_repo", "*")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        assert deploy.main(["--health-only"]) == 0


def test_main_health_only_requires_a_base_url(monkeypatch, capsys):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.main(["--health-only"]) == 2
    assert "base URL" in capsys.readouterr().err


def test_main_rejects_health_only_combined_with_sync_env(monkeypatch, capsys):
    monkeypatch.setattr(settings, "public_base_url", BASE)
    assert deploy.main(["--health-only", "--sync-env"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_run_checks_reports_all_twelve_in_order(runnable, monkeypatch):
    _stub_all_checks(monkeypatch, ["PASS"] * 12)
    results = deploy.run_checks(frozenset({"owner/repo"}), BASE)
    assert [r.name for r in results] == [
        "config", "pricing", "boot-creds-live", "github-app", "health", "database",
        "runtime-config", "provider", "provider-live", "api-key-live",
        "render-service", "uptime-pinger",
    ]


def test_an_exploding_check_becomes_a_fail_and_does_not_abort_the_run(runnable, monkeypatch):
    """A complete table is the deliverable; one broken check must not deprive
    the operator of the other eleven diagnoses (spec section 7.3)."""
    def _boom():
        raise ValueError("unexpected")

    _stub_all_checks(monkeypatch, ["PASS"] * 12)
    monkeypatch.setattr(deploy, "check_database", _boom)
    results = deploy.run_checks(frozenset({"owner/repo"}), BASE)
    assert len(results) == 12
    database = next(r for r in results if r.name == "database")
    assert database.status == "FAIL"
    assert "ValueError" in database.detail


@pytest.fixture
def gemini_only_config(complete_config, monkeypatch):
    """A first-time user's .env: gemini as the active provider (resolved from
    the DB, not settings.llm_provider -- see the fixture's psycopg stub
    below), with the other providers' keys listed but empty. Returns
    "gemini", the provider to pass to _wanted_env()/etc."""
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    # database_url is a dummy, unreachable host -- stub psycopg.connect so
    # _resolved_provider()'s read is deterministic rather than an accident of
    # DNS failure (tests must never open a real DB connection).
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(("gemini",)))
    return "gemini"


def test_wanted_env_pushes_the_selected_providers_credential_not_model(
    gemini_only_config, monkeypatch
):
    monkeypatch.setattr(settings, "gemini_model", "gemini-flash-latest")
    wanted = deploy._wanted_env(gemini_only_config)
    assert wanted["GEMINI_API_KEY"] == "gk_x"
    assert "GEMINI_MODEL" not in wanted  # model is DB-only (slot_config) now
    assert "LLM_PROVIDER" not in wanted  # provider is DB-only (runtime_config) now


def test_wanted_env_omits_unset_credentials_of_other_providers(gemini_only_config):
    """A Groq-only or Gemini-only .env must never be asked for another
    provider's key -- the whole point of opt-in provider config."""
    wanted = deploy._wanted_env(gemini_only_config)
    assert "GROQ_API_KEY" not in wanted


def test_wanted_env_includes_other_credentials_that_are_set(
    gemini_only_config, monkeypatch
):
    """Pushed when locally filled, so a later dashboard-side switch works."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    assert deploy._wanted_env(gemini_only_config)["GROQ_API_KEY"] == "gsk_x"


def test_wanted_env_includes_installation_id_when_set_locally(
    gemini_only_config, monkeypatch
):
    """Required -- always synced once configured."""
    monkeypatch.setattr(settings, "github_app_installation_id", 148449134)
    assert deploy._wanted_env(gemini_only_config)["GITHUB_APP_INSTALLATION_ID"] == "148449134"


def test_wanted_env_includes_installation_id_as_empty_when_unset(
    gemini_only_config, monkeypatch
):
    """Required, never guessed on the operator's behalf (ISSUES.md 2026-08-21)
    -- an unset (0) value must still appear in _wanted_env()'s output (as an
    empty string) so sync_env()'s generic "refuse to push empty values"
    guard catches it, rather than being silently omitted the way a
    genuinely optional setting (e.g. VERTEX_GCP_PROJECT) is."""
    monkeypatch.setattr(settings, "github_app_installation_id", 0)
    assert deploy._wanted_env(gemini_only_config)["GITHUB_APP_INSTALLATION_ID"] == ""


def test_generic_operational_env_attrs_is_now_empty(gemini_only_config):
    """ISSUES.md 2026-08-17's original 12 generic-operational keys are all
    DB-only now (Task 13, 2026-09-08 slotted-config-and-db-delegation) --
    moved to _DB_SYNCED_OPERATIONAL_KEYS. _GENERIC_OPERATIONAL_ENV_ATTRS is
    deliberately left in place, empty, as the landing spot for a future
    setting that genuinely needs this sync path -- see its own module
    comment. _wanted_env() must never mention any of the 12 either."""
    assert deploy._GENERIC_OPERATIONAL_ENV_ATTRS == {}
    wanted = deploy._wanted_env(gemini_only_config)
    for env_name in (
        "VERTEX_GCP_PROJECT", "VERTEX_GCP_LOCATION", "LLM_REQUEST_TIMEOUT_SECONDS",
        "DISPATCHER_IDLE_SLEEP_SECONDS", "DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS",
        "DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS", "DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS",
        "DISPATCHER_MAX_FAILURE_ATTEMPTS", "DISPATCHER_MAX_NOTICE_POST_ATTEMPTS",
        "DISPATCHER_MIN_RETRY_AFTER_SECONDS", "DISPATCHER_BACKOFF_JITTER_SECONDS",
        "DISPATCHER_NOTICE_SWEEP_BATCH_SIZE",
    ):
        assert env_name not in wanted


def test_wanted_env_never_includes_the_never_synced_operational_keys(gemini_only_config):
    """RENDER_SERVICE_NAME/PUBLIC_BASE_URL are operator-machine-only settings
    (config.py's own field comments) -- they must never reach Render."""
    wanted = deploy._wanted_env(gemini_only_config)
    for key in deploy._NEVER_SYNCED_OPERATIONAL_KEYS:
        assert key not in wanted


def test_wanted_env_never_includes_the_db_synced_operational_keys(gemini_only_config):
    """Usage-cap/cooldown settings sync straight into runtime_config (see
    sync_config_db()) -- they have no Render env var at all, so _wanted_env()
    (which only ever describes what --sync-env pushes to Render) must never
    mention them."""
    wanted = deploy._wanted_env(gemini_only_config)
    for key in deploy._DB_SYNCED_OPERATIONAL_KEYS:
        assert key not in wanted


def test_operational_keys_partition_cleanly_across_every_sync_destination():
    """CI-enforced version of the ISSUES.md 2026-08-17 ask: every name in
    OPERATIONAL_KEYS must land in exactly one sync destination, so a future
    setting added to OPERATIONAL_KEYS but forgotten everywhere else fails a
    test instead of silently never syncing anywhere, the way the original 12
    did."""
    handled_directly_in_wanted_env = {"GITHUB_TARGET_REPO"}
    # GEMINI_MODEL/GROQ_MODEL/VERTEX_MODEL are no longer pushed to Render at
    # all (active_model() is DB-only, slot_config) -- their one remaining
    # sync path is sync_env()'s _seed_slot_zero_config_if_missing(), which
    # reads them as the ONE-TIME seed value for the active provider's slot 0
    # (Task 13), not a per-sync push like every other group here.
    seeded_into_slot_zero_once = {"GEMINI_MODEL", "GROQ_MODEL", "VERTEX_MODEL"}
    groups = {
        "handled directly in _wanted_env()": handled_directly_in_wanted_env,
        "seeded into slot_config slot 0 once": seeded_into_slot_zero_once,
        "_GENERIC_OPERATIONAL_ENV_ATTRS": set(deploy._GENERIC_OPERATIONAL_ENV_ATTRS),
        "_DB_SYNCED_OPERATIONAL_KEYS": set(deploy._DB_SYNCED_OPERATIONAL_KEYS),
        "_NEVER_SYNCED_OPERATIONAL_KEYS": set(deploy._NEVER_SYNCED_OPERATIONAL_KEYS),
    }
    union = set().union(*groups.values())
    missing = OPERATIONAL_KEYS - union
    extra = union - OPERATIONAL_KEYS
    assert not missing, f"in OPERATIONAL_KEYS but in no sync destination: {sorted(missing)}"
    assert not extra, f"in a sync destination but not OPERATIONAL_KEYS: {sorted(extra)}"
    seen: set[str] = set()
    for label, group in groups.items():
        overlap = seen & group
        assert not overlap, f"{label} overlaps an earlier group on: {sorted(overlap)}"
        seen |= group


def test_sync_env_does_not_demand_other_providers_keys(
    gemini_only_config, monkeypatch, capsys
):
    """The regression this task exists for: the default config could not sync
    at all, and the error named two providers the user never chose."""
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    code = deploy.sync_env()
    err = capsys.readouterr().err
    assert "GROQ_API_KEY" not in err
    assert code == 1          # got past the guards, failed on the missing service


def test_sync_env_refuses_when_the_selected_credential_is_empty(
    gemini_only_config, monkeypatch, capsys
):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    called = []
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    assert called == []       # refused before any HTTP


@pytest.fixture
def sync_ready(monkeypatch):
    """Every value _wanted_env() reads, non-empty, plus a Render key."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h:5432/postgres")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key", "aGVsbG8=")
    monkeypatch.setattr(settings, "github_app_installation_id", 148449134)
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(settings, "dashboard_username", "dash-user")
    monkeypatch.setattr(settings, "dashboard_password", "dash-pass")
    monkeypatch.setattr(settings, "dashboard_session_secret", "dash-session-secret")
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
    # database_url is a dummy, unreachable host -- stub psycopg.connect so
    # _resolved_provider()'s read ("groq" is active) is deterministic rather
    # than an accident of DNS failure (tests must never open a real DB
    # connection).
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(("groq",)))


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sync_env_requires_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.sync_env() == 2


@pytest.mark.parametrize(
    "local_url",
    [
        # exactly what scripts/test_db.py exports:
        "postgresql://postgres:x@localhost:5433/postgres",
        "postgresql://postgres:x@127.0.0.1:5432/postgres",
        "postgresql://u:p@db.internal:5432/postgres",
    ],
)
def test_sync_env_refuses_a_local_test_database_url(sync_ready, monkeypatch, capsys, local_url):
    """DATABASE_URL is always-synced and Settings reads os.environ ahead of
    .env, so a shell that ran `eval "$(uv run python -m scripts.test_db)"`
    would otherwise repoint the live Render service at a throwaway container
    on the operator's laptop."""
    monkeypatch.setattr(settings, "database_url", local_url)
    assert deploy.sync_env() == 2
    err = capsys.readouterr().err
    assert "refusing to sync" in err
    assert "scripts.test_db" in err
    assert "unset DATABASE_URL" in err


def test_sync_env_local_database_url_guard_refuses_before_any_request(sync_ready, monkeypatch):
    """Same 'refuse before touching anything' contract the other pre-push
    guards hold to -- no Render call, and no psycopg connection to the local
    database either."""
    monkeypatch.setattr(settings, "database_url", "postgresql://postgres:x@localhost:5433/postgres")
    connected = []
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: connected.append(1))
    with respx.mock:
        services = respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list())
        )
        assert deploy.sync_env() == 2
    assert services.call_count == 0
    assert connected == []


def test_sync_env_still_accepts_a_real_remote_database_url(sync_ready, capsys):
    """The guard must not fire on a normal hosted DATABASE_URL -- sync_ready's
    host is a plain remote hostname, so a full sync still runs to completion."""
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "live"}})
        )
        assert deploy.sync_env() == 0
    assert "refusing to sync" not in capsys.readouterr().err


def test_sync_env_local_db_guard_consumes_the_shared_prereqs_predicate():
    """The guard must never grow its own private notion of 'local' -- it reads
    scripts/_prereqs.py's predicate, the same one scripts/test_db.py uses."""
    assert deploy._looks_like_local_test_db is _prereqs._looks_like_local_test_db


def test_sync_env_refuses_to_push_an_empty_value(sync_ready, monkeypatch, capsys):
    """A blank .env entry must never overwrite a working remote secret, and the
    guard must fire before any request is issued."""
    monkeypatch.setattr(settings, "groq_api_key", "")
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list())
        )
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_sync_env_refuses_to_push_without_an_installation_id(sync_ready, monkeypatch, capsys):
    """GITHUB_APP_INSTALLATION_ID is required, never guessed on the
    operator's behalf (ISSUES.md 2026-08-21) -- an unset value must refuse
    to push, same shape as any other missing required credential."""
    monkeypatch.setattr(settings, "github_app_installation_id", 0)
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list())
        )
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GITHUB_APP_INSTALLATION_ID" in capsys.readouterr().err


def test_sync_env_refuses_an_empty_target_repo(sync_ready, monkeypatch, capsys):
    """An empty GITHUB_TARGET_REPO used to be a valid, deliberate "track all
    repos" config exempt from the empty-value guard; it's now unconfigured
    -- "*" is the explicit spelling of track-all (2026-09-07), so it must
    trip the guard like any other required key."""
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    code = deploy.sync_env()
    err = capsys.readouterr().err
    assert "GITHUB_TARGET_REPO" in err
    assert code == 2


def test_sync_env_pushes_a_target_repo_of_star_without_tripping_the_empty_guard(
    sync_ready, monkeypatch, capsys
):
    """"*" is a real, non-empty value -- it goes through the ordinary PUT
    path like any other required key, no DELETE special-casing needed."""
    monkeypatch.setattr(settings, "github_target_repo", "*")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    code = deploy.sync_env()
    err = capsys.readouterr().err
    assert "GITHUB_TARGET_REPO" not in err
    assert code == 1          # got past the empty-value guard, failed on the missing service


def test_optional_empty_env_keys_is_now_empty():
    """VERTEX_GCP_PROJECT (the one entry this ever held) is DB-only now
    (slot_config) and never appears in _wanted_env()'s output -- see
    _OPTIONAL_EMPTY_ENV_KEYS's own module comment. The DELETE-vs-PUT
    empty-value handling this used to exercise
    (test_sync_env_deletes_rather_than_puts_an_empty_optional_key,
    test_sync_env_treats_a_404_on_delete_as_already_unset,
    test_sync_env_treats_an_already_absent_optional_key_as_in_sync -- all
    removed) has no real key left to trigger it."""
    assert deploy._OPTIONAL_EMPTY_ENV_KEYS == frozenset()


def test_sync_env_refuses_gemini_provider_with_no_synced_gemini_key(
    sync_ready, monkeypatch, capsys
):
    """gemini selected (runtime_config.provider has no implicit default --
    design spec 2026-08-18 section 6e, generalized to the DB by
    2026-09-09's provider-key-index-db-only design); if the selected
    provider's own credential is empty locally, syncing would push a
    service that boots and answers /healthz while failing every real
    review, with every checklist check reporting green. The guard must
    fire before any request."""
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(("gemini",)))
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list())
        )
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_sync_env_reports_a_partial_push_as_exit_one_not_could_not_run(
    sync_ready, capsys
):
    """Once one PUT has actually landed on the service, a later failure is a
    PARTIAL push, not a failure to start -- it must exit 1 and name what was
    already pushed, never exit 2's 'could not run at all', which would leave
    an operator thinking the half-configured service is untouched."""
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        wanted = deploy._wanted_env("groq")
        current = dict.fromkeys(wanted, "stale")
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(current))
        )
        first_key, second_key = list(wanted)[:2]
        respx.put(f"{RENDER_SERVICES}/srv-1/env-vars/{first_key}").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.put(f"{RENDER_SERVICES}/srv-1/env-vars/{second_key}").mock(
            side_effect=httpx.ConnectError("boom")
        )
        code = deploy.sync_env()
    err = capsys.readouterr().err
    assert code == 1
    assert first_key in err            # names what was actually pushed
    assert second_key not in err       # never claims the failed one landed
    assert "partial" in err.lower()


def test_sync_env_pushes_only_changed_keys_via_the_single_key_endpoint(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        wanted = deploy._wanted_env("groq")
        current = dict.fromkeys(wanted, "stale")
        # _OPTIONAL_EMPTY_ENV_KEYS is empty now (Task 13), so there is no
        # longer any key here that would take the DELETE branch instead of
        # this test's PUT endpoint.
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(current))
        )
        bulk = respx.put(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json={})
        )
        single = respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])  # nothing in flight
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1", "status": "created"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "live"}})
        )
        assert deploy.sync_env() == 0
        assert not bulk.called          # the bulk PUT would delete DATABASE_URL
        # All but the already-correct _OPTIONAL_EMPTY_ENV_KEYS differ.
        assert single.call_count == len(wanted) - len(deploy._OPTIONAL_EMPTY_ENV_KEYS)


def test_sync_env_skips_the_deploy_when_nothing_changed(sync_ready, capsys):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(deploy._wanted_env("groq")))
        )
        triggered = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={})
        )
        assert deploy.sync_env() == 0
        assert not triggered.called
    assert "already in sync" in capsys.readouterr().out


def test_sync_env_fails_when_the_deploy_fails(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])  # nothing in flight
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "build_failed"}})
        )
        assert deploy.sync_env() == 1


def test_canceled_is_not_treated_as_a_build_failure():
    """Cancellation is what a superseding deploy looks like, not a failure."""
    assert "canceled" not in deploy._DEPLOY_FAILED_STATUSES
    assert "build_failed" in deploy._DEPLOY_FAILED_STATUSES


def test_deploy_timeout_allows_for_a_cold_docker_build():
    assert deploy._DEPLOY_TIMEOUT_SECONDS >= 900


@respx.mock
def test_trigger_and_wait_reports_a_superseded_deploy_distinctly(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    respx.post("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=httpx.Response(201, json={"id": "dep-1"})
    )
    respx.get("https://api.render.com/v1/services/svc-1/deploys/dep-1").mock(
        return_value=httpx.Response(200, json={"status": "canceled"})
    )
    code = deploy._trigger_and_wait("svc-1")
    err = capsys.readouterr().err
    assert code == 1
    assert "superseded" in err
    assert "env vars" in err          # says what did happen, not just what failed


@respx.mock
def test_sync_env_waits_for_an_in_flight_deploy_before_triggering(monkeypatch):
    """Triggering on top of a running build stacks two; waiting guarantees the
    pushed values are in the live container."""
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    statuses = iter([
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "live"}}],
    ])
    route = respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        side_effect=lambda request: httpx.Response(200, json=next(statuses))
    )
    deploy._wait_for_in_flight("svc-1")
    assert route.call_count == 2


@respx.mock
def test_wait_for_in_flight_prints_periodic_progress_during_a_long_wait(monkeypatch, capsys):
    """The initial announcement and the timeout message are covered elsewhere;
    this covers the periodic line in between so a long real wait is never
    silent for more than _IN_FLIGHT_PROGRESS_EVERY polls."""
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    monkeypatch.setattr(deploy, "_IN_FLIGHT_PROGRESS_EVERY", 2)
    statuses = iter([
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "live"}}],
    ])
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        side_effect=lambda request: httpx.Response(200, json=next(statuses))
    )
    assert deploy._wait_for_in_flight("svc-1") is True
    out = capsys.readouterr().out
    assert out.count("still waiting for in-flight deploy") >= 1


def test_sync_env_triggers_a_fresh_deploy_after_the_in_flight_one_settles(sync_ready):
    """The real property under review: observe an in-flight deploy, wait for it
    to settle, then issue a BRAND-NEW trigger -- never adopt the one observed.
    If the code adopted dep-old instead, it would poll GET .../deploys/dep-old,
    which is not mocked here, and sync_env would return 2, not 0."""
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        in_flight = iter([
            [{"deploy": {"id": "dep-old", "status": "build_in_progress"}}],
            [{"deploy": {"id": "dep-old", "status": "live"}}],
        ])
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            side_effect=lambda request: httpx.Response(200, json=next(in_flight))
        )
        new_deploy = {"deploy": {"id": "dep-new", "status": "created"}}
        trigger = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json=new_deploy)
        )
        poll_new = respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-new").mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-new", "status": "live"}})
        )
        assert deploy.sync_env() == 0
        assert trigger.called
        assert poll_new.called          # polled the NEW id, not the observed dep-old


def test_sync_env_times_out_waiting_for_in_flight_and_refuses_to_trigger(
    sync_ready, monkeypatch, capsys
):
    """The regression this fix round exists for: a timed-out wait must not
    silently fall through into triggering a second deploy on top of one still
    building. The assertion on the POST route's call_count is the one that
    would have caught it."""
    monkeypatch.setattr(deploy, "_DEPLOY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        stuck = [{"deploy": {"id": "dep-stuck", "status": "build_in_progress"}}]
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=stuck)
        )
        trigger = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-new"}})
        )
        code = deploy.sync_env()
    err = capsys.readouterr().err
    assert code == 1
    assert "timed out" in err
    assert "in-flight" in err
    assert trigger.call_count == 0


def test_sync_env_never_prints_a_secret_value(sync_ready, monkeypatch, capsys):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])  # nothing in flight
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "live"}})
        )
        deploy.sync_env()
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET" not in captured.out + captured.err


def test_sync_config_db_requires_a_database_url(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.sync_config_db() == 2
    assert "DATABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "local_url",
    [
        "postgresql://postgres:x@localhost:5433/postgres",
        "postgresql://postgres:x@127.0.0.1:5432/postgres",
        "postgresql://u:p@db.internal:5432/postgres",
    ],
)
def test_sync_config_db_refuses_a_local_test_database_url(monkeypatch, capsys, local_url):
    """Same shape as sync_env()'s guard (ISSUES.md Parked Issues) -- run from
    a shell where `eval "$(uv run python -m scripts.test_db)"` is still
    exported, this would otherwise silently write config into the throwaway
    local container while an operator believes it reached production."""
    monkeypatch.setattr(settings, "database_url", local_url)
    connected = []
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: connected.append(1))
    assert deploy.sync_config_db() == 2
    err = capsys.readouterr().err
    assert "refusing to sync" in err
    assert connected == []


def test_sync_config_db_still_accepts_a_real_remote_database_url(monkeypatch, capsys):
    """The guard must not fire on a normal hosted DATABASE_URL."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    assert deploy.sync_config_db() == 0
    assert "refusing to sync" not in capsys.readouterr().err


@pytest.fixture
def _real_db_target(db, monkeypatch):
    """The `db` fixture's URL is local-shaped by construction (a real
    Postgres for tests to verify actual SQL against) -- exactly what the
    local-DB guard above exists to refuse. Bypass it for tests that
    deliberately need real Postgres, without weakening the guard itself for
    the operator-mistake scenario it protects against in production."""
    monkeypatch.setattr(deploy, "_looks_like_local_test_db", lambda url: False)
    yield


def test_sync_config_db_refuses_a_base_above_the_cap(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 4000.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    assert deploy.sync_config_db() == 2
    assert "refusing to sync" in capsys.readouterr().err


def test_sync_config_db_refuses_a_non_positive_base(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 0.0)
    assert deploy.sync_config_db() == 2
    assert "refusing to sync" in capsys.readouterr().err


def test_sync_config_db_refuses_a_non_positive_cap(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", -1.0)
    assert deploy.sync_config_db() == 2
    assert "refusing to sync" in capsys.readouterr().err


def test_sync_config_db_never_reaches_the_database_when_invalid(monkeypatch, capsys):
    """The guard runs before any connection attempt -- mirrors sync_env()'s
    own 'refuse before touching anything' guards."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 0.0)
    called = []
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: called.append(1))
    assert deploy.sync_config_db() == 2
    assert called == []


def test_sync_config_db_writes_settings_values_into_runtime_config(
    _real_db_target, db_query, monkeypatch
):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 45.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 900.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 1.5)
    monkeypatch.setattr(settings, "key_usage_token_cap", 20000)
    assert deploy.sync_config_db() == 0
    row = db_query(
        "SELECT cooldown_base_seconds, cooldown_max_seconds, cooldown_factor, "
        "key_usage_token_cap, key_usage_reset_time_utc "
        "FROM runtime_config WHERE id = 1"
    )[0]
    assert row == (45.0, 900.0, 1.5, 20000, "04:00:00")


def test_sync_config_db_writes_every_tuning_knob_into_runtime_config(
    _real_db_target, db_query, monkeypatch
):
    """Regression test for the whole finding: --sync-config-db used to never
    write any of the 9 tuning knobs (or DISPATCHER_IDLE_SLEEP_SECONDS) into
    runtime_config at all -- editing them in .env.config and running this
    was a complete no-op."""
    overrides = {
        "llm_request_timeout_seconds": 12.0,
        "dispatcher_default_retry_after_seconds": 13.0,
        "dispatcher_failure_base_backoff_seconds": 14.0,
        "dispatcher_failure_max_backoff_seconds": 900.0,
        "dispatcher_max_failure_attempts": 7,
        "dispatcher_max_notice_post_attempts": 4,
        "dispatcher_min_retry_after_seconds": 15.0,
        "dispatcher_backoff_jitter_seconds": 16.0,
        "dispatcher_notice_sweep_batch_size": 25,
        "dispatcher_idle_sleep_seconds": 17.0,
    }
    for attr, value in overrides.items():
        monkeypatch.setattr(settings, attr, value)
    assert deploy.sync_config_db() == 0
    row = db_query(
        "SELECT " + ", ".join(overrides) + " FROM runtime_config WHERE id = 1"
    )[0]
    assert row == tuple(overrides.values())


def test_sync_config_db_refuses_an_invalid_tuning_knob(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    monkeypatch.setattr(settings, "dispatcher_notice_sweep_batch_size", 0)
    assert deploy.sync_config_db() == 2
    err = capsys.readouterr().err
    assert "refusing to sync" in err
    assert "dispatcher_notice_sweep_batch_size" in err


def test_sync_config_db_refuses_a_non_positive_idle_sleep(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    monkeypatch.setattr(settings, "dispatcher_idle_sleep_seconds", 0.0)
    assert deploy.sync_config_db() == 2
    err = capsys.readouterr().err
    assert "refusing to sync" in err
    assert "DISPATCHER_IDLE_SLEEP_SECONDS" in err


def test_sync_config_db_writes_review_draft_prs_into_runtime_config(
    _real_db_target, db_query, monkeypatch
):
    monkeypatch.setattr(settings, "review_draft_prs", True)
    assert deploy.sync_config_db() == 0
    row = db_query("SELECT review_draft_prs FROM runtime_config WHERE id = 1")[0]
    assert row == (True,)


def test_sync_config_db_refuses_with_an_actionable_message_when_a_column_is_missing(
    _real_db_target, _runtime_config_missing_review_draft_prs, capsys
):
    """Reproduces the real incident: review_draft_prs missing from an
    already-provisioned runtime_config used to surface as a raw
    UndefinedColumn from the INSERT below. It must instead refuse early with
    the exact ALTER TABLE fix, and must never reach that INSERT at all."""
    assert deploy.sync_config_db() == 2
    err = capsys.readouterr().err
    assert "review_draft_prs" in err
    assert (
        "ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS review_draft_prs BOOLEAN" in err
    )
    assert "UndefinedColumn" not in err


def test_sync_config_db_reports_already_in_sync_on_a_second_run(_real_db_target, capsys):
    assert deploy.sync_config_db() == 0
    capsys.readouterr()
    assert deploy.sync_config_db() == 0
    assert "already in sync" in capsys.readouterr().out


def test_sync_config_db_reports_only_the_fields_that_changed(_real_db_target, monkeypatch, capsys):
    assert deploy.sync_config_db() == 0
    capsys.readouterr()
    monkeypatch.setattr(settings, "key_usage_token_cap", 50000)
    assert deploy.sync_config_db() == 0
    out = capsys.readouterr().out
    assert "key_usage_token_cap" in out
    assert "cooldown_base_seconds" not in out  # unchanged, not reported


def test_sync_config_db_an_existing_db_override_is_overwritten(
    _real_db_target, db_exec, db_query, monkeypatch
):
    """.env.config is the source of truth (2026-08-17 design note): a
    previously-set live override (however it got there) is unconditionally
    replaced by whatever settings currently resolves to."""
    db_exec(
        "INSERT INTO runtime_config (id, key_usage_token_cap, updated_at) "
        "VALUES (1, 999999, '2026-01-01T00:00:00+00:00')"
    )
    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    assert deploy.sync_config_db() == 0
    row = db_query("SELECT key_usage_token_cap FROM runtime_config WHERE id = 1")[0]
    assert row == (None,)


def test_seed_slot_zero_config_seeds_when_missing(_real_db_target, db_query, monkeypatch):
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    deploy._seed_slot_zero_config_if_missing("groq")
    row = db_query(
        "SELECT model, vertex_gcp_project, vertex_gcp_location "
        "FROM slot_config WHERE provider = 'groq' AND slot_index = 0"
    )
    assert row == [("llama-3.3-70b-versatile", None, None)]


def test_seed_slot_zero_config_is_a_no_op_when_a_row_already_exists(
    _real_db_target, db_exec, db_query, monkeypatch
):
    """An operator's later dashboard/guided-setup edit to slot 0 must never
    be silently overwritten by a stale local .env.config value on a later
    --sync-env run."""
    monkeypatch.setattr(settings, "groq_model", "some-other-model")
    db_exec(
        "INSERT INTO slot_config (provider, slot_index, model, updated_at) "
        "VALUES ('groq', 0, 'operator-set-model', '2026-01-01T00:00:00+00:00')"
    )
    deploy._seed_slot_zero_config_if_missing("groq")
    row = db_query(
        "SELECT model FROM slot_config WHERE provider = 'groq' AND slot_index = 0"
    )
    assert row == [("operator-set-model",)]


def test_seed_slot_zero_config_does_nothing_for_an_unknown_provider(monkeypatch):
    connected = []
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: connected.append(1))
    deploy._seed_slot_zero_config_if_missing("bogus")
    assert connected == []


def test_sync_config_db_prints_a_render_reachability_line_without_a_key(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    assert deploy.sync_config_db() == 0
    assert "no RENDER_API_KEY" in capsys.readouterr().out


def test_main_sync_config_db_runs_without_a_public_base_url(monkeypatch):
    """Unlike every other mode, --sync-config-db needs no PUBLIC_BASE_URL --
    it never talks to the deployed app itself, only the database."""
    monkeypatch.setattr(deploy, "sync_config_db", lambda: 0)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "")
    assert deploy.main(["--sync-config-db"]) == 0


def test_main_sync_config_db_propagates_a_nonzero_exit_code(monkeypatch):
    monkeypatch.setattr(deploy, "sync_config_db", lambda: 2)
    assert deploy.main(["--sync-config-db"]) == 2


def test_main_sync_config_db_skips_the_checklist(monkeypatch):
    """Only sync_config_db() runs -- no health/render/database/etc. checks."""
    monkeypatch.setattr(deploy, "sync_config_db", lambda: 0)
    called = []
    monkeypatch.setattr(deploy, "run_checks", lambda *a, **k: called.append(1))
    deploy.main(["--sync-config-db"])
    assert called == []


def test_main_rejects_sync_config_db_combined_with_sync_env(monkeypatch, capsys):
    assert deploy.main(["--sync-config-db", "--sync-env"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_rejects_sync_config_db_combined_with_health_only(monkeypatch, capsys):
    assert deploy.main(["--sync-config-db", "--health-only"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_wanted_env_is_always_a_superset_of_the_always_synced_names():
    """_ALWAYS_SYNCED is what the docs test validates against, so _wanted_env()
    must always include it regardless of the selected provider -- otherwise the
    docs test would silently stop covering vars that are actually pushed. Unlike
    the old fixed eight-name set, exact equality no longer holds: _wanted_env()
    also carries the selected provider's credential and any other provider's
    credential that happens to be set locally."""
    assert set(deploy._ALWAYS_SYNCED) <= set(deploy._wanted_env("groq"))


def test_wanted_env_pushes_a_numbered_slot_with_a_local_value(gemini_only_config, monkeypatch):
    monkeypatch.setattr(
        deploy._override, "local_slot_values",
        lambda base: {"GEMINI_API_KEY_1": "gk_slot1"} if base == "GEMINI_API_KEY" else {},
    )
    wanted = deploy._wanted_env(gemini_only_config)
    assert wanted["GEMINI_API_KEY_1"] == "gk_slot1"


def test_wanted_env_omits_a_numbered_slot_with_no_local_value(gemini_only_config, monkeypatch):
    monkeypatch.setattr(deploy._override, "local_slot_values", lambda base: {})
    wanted = deploy._wanted_env(gemini_only_config)
    assert "GEMINI_API_KEY_1" not in wanted
    assert "GROQ_API_KEY_1" not in wanted


def test_wanted_env_pushes_numbered_slots_for_every_provider_not_just_the_selected_one(
    gemini_only_config, monkeypatch
):
    """Mirrors the existing 'other credentials pushed when locally filled'
    policy (test_wanted_env_includes_other_credentials_that_are_set) --
    extended to numbered slots."""
    def _slots(base):
        if base == "GROQ_API_KEY":
            return {"GROQ_API_KEY_2": "gsk_slot2"}
        return {}

    monkeypatch.setattr(deploy._override, "local_slot_values", _slots)
    wanted = deploy._wanted_env(gemini_only_config)
    assert wanted["GROQ_API_KEY_2"] == "gsk_slot2"


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_render_yaml_declares_every_synced_var():
    """A fresh Blueprint provision only creates env-var slots for names
    render.yaml declares -- a name --sync-env can push but render.yaml omits
    would silently fail to authenticate on a brand-new deploy."""
    render = yaml.safe_load((_REPO_ROOT / "render.yaml").read_text())
    declared = {
        entry["key"] for entry in render["services"][0]["envVars"] if "key" in entry
    }
    names = set(deploy._ALWAYS_SYNCED) | set(deploy._GENERIC_OPERATIONAL_ENV_ATTRS)
    # Model vars are no longer expected in render.yaml at all: active_model()
    # is DB-only now (slot_config), so a model var is never pushed to Render.
    for credential, _model_var in deploy._PROVIDERS.values():
        names.add(credential)
    assert names <= declared, f"missing from render.yaml: {sorted(names - declared)}"


def test_render_yaml_never_declares_a_db_synced_key():
    """The 5 usage-cap/cooldown keys are never a Render env var at all (see
    _DB_SYNCED_OPERATIONAL_KEYS) -- re-declaring one here would silently
    resurrect the two-sources-of-truth problem this design eliminated."""
    render = yaml.safe_load((_REPO_ROOT / "render.yaml").read_text())
    declared = {
        entry["key"] for entry in render["services"][0]["envVars"] if "key" in entry
    }
    overlap = declared & deploy._DB_SYNCED_OPERATIONAL_KEYS
    assert not overlap, f"declared in render.yaml but DB-synced only: {sorted(overlap)}"


def test_exit_codes_are_documented():
    """Spec section 7.2 lists three causes for exit 2; the docs must carry
    them, or the contract exists only in the code."""
    text = (_REPO_ROOT / "guide" / "operations" / "deploy.md").read_text(encoding="utf-8")
    assert "exit 0" in text and "exit 1" in text and "exit 2" in text


def test_main_rejects_an_unknown_flag(monkeypatch, capsys):
    """A typo must not silently degrade to a checks-only run that reports
    success for a sync that never happened."""
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--sync-en"])
    assert exc.value.code == 2
    assert "--sync-en" in capsys.readouterr().err


def test_main_supports_help(capsys):
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--help"])
    assert exc.value.code == 0
    assert "--sync-env" in capsys.readouterr().out


def test_operator_api_keys_are_blank_by_default():
    """A test that forgets to monkeypatch these must hit no live API. Task 2
    overwrote a live Render env var because this guard did not exist."""
    assert settings.render_api_key == ""
    assert settings.uptimerobot_api_key == ""


def test_check_render_service_fails_rather_than_calling_out(monkeypatch):
    """With the keys quarantined, the Render check now FAILs -- it can never
    reach api.render.com from a default test run, and a missing mandatory
    key is a failure, not a skip."""
    result = deploy.check_render_service()
    assert result.status == "FAIL"


class _FakeCursor:
    """Stands in for the cursor psycopg.Connection.execute() returns."""

    def __init__(self, outcome):
        self._outcome = outcome

    def fetchone(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _FakeConn:
    """A context-manager connection whose .execute().fetchone() is scripted --
    _resolved_provider() reads via a raw psycopg.connect(), not the store's
    pool, so the seam to fake is psycopg.connect itself."""

    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self, *_args, **_kwargs):
        return _FakeCursor(self._outcome)

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


@pytest.fixture
def override_seam(complete_config, monkeypatch):
    """check_provider (and sync_env) resolve the active provider via a raw,
    short-timeout psycopg.connect() -- mirroring why check_database avoids
    store.init_pool()'s 30s pool timeout in a one-shot CLI. This fakes that
    connection so tests stay offline; call the fixture with the row
    fetchone() should return (a 1-tuple, or None for no row), or with an
    exception instance to simulate a query failure (e.g. a missing table).
    Use psycopg.OperationalError for that, not RuntimeError -- RuntimeError
    is _resolved_provider()'s own signal for "provider unset", so a fake
    connectivity failure using the same type would be indistinguishable from
    it (a real psycopg failure never raises bare RuntimeError).

    complete_config does not set a DATABASE_URL, so this also supplies one --
    without it check_provider SKIPs before ever reaching psycopg.connect.
    Defaults to a resolved ("groq",) row, since provider is DB-only now (no
    env fallback) -- most tests need a valid provider to get anywhere past
    that resolution."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")

    def _set(outcome):
        monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(outcome))

    _set(("groq",))
    return _set


def test_check_provider_reports_the_active_provider(override_seam):
    override_seam(("groq",))
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert result.detail == "groq"


def test_check_provider_reports_a_different_active_provider(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    override_seam(("gemini",))
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert result.detail == "gemini"


def test_check_provider_fails_when_the_credential_is_missing(override_seam):
    """Green rows on a service failing every review is the exact failure this
    check exists to prevent."""
    override_seam(("gemini",))  # gemini_api_key left unset by autouse fixture
    result = deploy.check_provider()
    assert result.status == "FAIL"
    assert "GEMINI_API_KEY" in result.detail


def test_check_provider_fails_when_no_provider_is_set(override_seam):
    override_seam(None)
    result = deploy.check_provider()
    assert result.status == "FAIL"
    assert "runtime_config.provider is unset" in result.detail


def test_check_provider_treats_an_empty_string_as_no_provider(override_seam):
    """store.get_provider_override() collapses '' to None
    (tests/test_provider_override.py::test_an_empty_provider_string_reads_as_no_override
    pins this). The raw read here must match, or the CLI and the dispatcher
    would disagree about whether a provider is active."""
    override_seam(("",))
    result = deploy.check_provider()
    assert result.status == "FAIL"
    assert "unset" in result.detail


def test_check_provider_skips_when_the_read_raises(override_seam):
    """A database the app has never booted against has no runtime_config
    table yet -- the query raises. That is `database`'s row to report, not
    provider's, so this must SKIP rather than blow up."""
    override_seam(psycopg.OperationalError("relation \"runtime_config\" does not exist"))
    result = deploy.check_provider()
    assert result.status == "SKIPPED"


def test_check_provider_skips_without_a_database_url(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.check_provider().status == "SKIPPED"


def test_provider_live_fails_without_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    result = deploy.check_provider_live()
    assert result.status == "FAIL"
    assert "RENDER_API_KEY" in result.detail


def test_provider_live_skips_without_a_database_url(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.check_provider_live().status == "SKIPPED"


def test_provider_live_skips_when_the_read_raises(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    override_seam(psycopg.OperationalError("boom"))
    assert deploy.check_provider_live().status == "SKIPPED"


def test_provider_live_fails_when_no_provider_is_set(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    override_seam(None)
    result = deploy.check_provider_live()
    assert result.status == "FAIL"
    assert "runtime_config.provider is unset" in result.detail


def test_provider_live_fails_when_the_credential_is_missing_on_render(
    override_seam, monkeypatch
):
    """The exact failure hit live during the demo rehearsal: `provider` PASSes
    locally while `provider-live` catches that Render was never given the key."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")  # present locally
    override_seam(("gemini",))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_provider_live()
    assert result.status == "FAIL"
    assert "GEMINI_API_KEY" in result.detail
    assert "not present" in result.detail


def test_provider_live_never_leaks_a_fetched_value(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    override_seam(("groq",))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"GROQ_API_KEY": "gsk_SUPER_SECRET"})
            )
        )
        result = deploy.check_provider_live()
    assert "gsk_SUPER_SECRET" not in result.detail
    assert result.detail.endswith("groq -- GROQ_API_KEY present on Render")


def test_resolved_key_index_or_env_falls_back_without_a_database_url(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy._resolved_key_index_or_env("groq") == (0, None)


def test_resolved_key_index_or_env_resolves_the_override_when_database_url_is_set(
    override_seam,
):
    override_seam((2,))
    assert deploy._resolved_key_index_or_env("groq") == (2, 2)


def test_resolved_key_index_or_env_defaults_to_zero_when_no_override(override_seam):
    override_seam(None)
    assert deploy._resolved_key_index_or_env("groq") == (0, None)


def test_resolved_key_index_or_env_propagates_a_db_error(override_seam):
    override_seam(psycopg.OperationalError("boom"))
    with pytest.raises(psycopg.OperationalError):
        deploy._resolved_key_index_or_env("groq")


def test_api_key_live_fails_without_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    result = deploy.check_api_key_live()
    assert result.status == "FAIL"
    assert "RENDER_API_KEY" in result.detail


def test_api_key_live_skips_when_the_provider_resolution_raises(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")

    def boom():
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(deploy, "_resolved_provider", boom)
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_skips_when_the_index_resolution_raises(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: "groq")

    def boom(provider):
        raise RuntimeError("db down")

    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", boom)
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_skips_for_an_unsupported_provider(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: "unknown")
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_passes_for_index_zero_present_on_render(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: "groq")
    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", lambda provider: (0, None))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_api_key_live()
    assert result.status == "PASS"
    assert "GROQ_API_KEY" in result.detail
    assert "GROQ_API_KEY_" not in result.detail  # index 0 -> unsuffixed name


def test_api_key_live_fails_when_the_overrides_slot_is_missing_on_render(monkeypatch):
    """The exact failure mode this check exists to catch: the DB override
    names index 2 but nobody ever pushed GROQ_API_KEY_2 to Render."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: "groq")
    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", lambda provider: (2, 2))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_api_key_live()
    assert result.status == "FAIL"
    assert "GROQ_API_KEY_2" in result.detail
    assert "not present" in result.detail


def test_api_key_live_never_leaks_a_fetched_value(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: "groq")
    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", lambda provider: (0, None))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"GROQ_API_KEY": "gsk_SUPER_SECRET"})
            )
        )
        result = deploy.check_api_key_live()
    assert "gsk_SUPER_SECRET" not in result.detail


def test_run_checks_includes_the_api_key_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_api_key_live",
                        lambda: deploy.CheckResult("api-key-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_boot_credentials_live", "boot-creds-live"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_provider_live", "provider-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn, lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "api-key-live" in names
    assert names.index("api-key-live") > names.index("provider-live")


def test_run_checks_includes_the_provider_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider_live",
                        lambda: deploy.CheckResult("provider-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_boot_credentials_live", "boot-creds-live"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_api_key_live", "api-key-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn, lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider-live" in names
    assert names.index("provider-live") > names.index("provider")


def test_run_checks_includes_the_provider_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider",
                        lambda: deploy.CheckResult("provider", "PASS", ""))
    # fn -> the row name it reports as, mirroring _stub_all_checks above.
    for fn, row in (
        ("check_config", "config"),
        ("check_boot_credentials_live", "boot-creds-live"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider_live", "provider-live"),
        ("check_api_key_live", "api-key-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn,
                            lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider" in names
    assert names.index("provider") > names.index("database")


def test_run_checks_includes_the_boot_creds_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_boot_credentials_live",
                        lambda: deploy.CheckResult("boot-creds-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_provider_live", "provider-live"),
        ("check_api_key_live", "api-key-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn,
                            lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "boot-creds-live" in names
    assert names.index("boot-creds-live") > names.index("config")


def test_wanted_env_never_pushes_any_providers_model_var(monkeypatch):
    """Superseded by slot_config -- active_model() is DB-only now, so no
    provider's model var (not even the currently-selected one's) is ever
    pushed to Render. Was test_wanted_env_pushes_every_providers_model_var."""
    from config import settings
    from scripts import deploy

    monkeypatch.setattr(settings, "gemini_model", "model-gemini")
    monkeypatch.setattr(settings, "groq_model", "model-groq")
    monkeypatch.setattr(settings, "vertex_model", "model-vertex")
    monkeypatch.setattr(settings, "github_app_private_key", "pem-b64")
    wanted = deploy._wanted_env("vertex")
    assert "GEMINI_MODEL" not in wanted
    assert "GROQ_MODEL" not in wanted
    assert "VERTEX_MODEL" not in wanted


def test_sync_env_no_longer_has_a_model_override_disagreement_guard():
    """The old model-override-disagreement guard (a Render model var could
    disagree with an active DB model override) no longer applies:
    active_model() is DB-only now (slot_config), and _wanted_env() never
    pushes a model var to Render at all -- see docs/superpowers/specs/
    2026-09-08-slotted-config-and-db-delegation-design.md section 6.
    Asserted against the source, matching
    test_sync_env_no_longer_refuses_an_unpriced_model's approach: the
    matched fragment is the f-string literal unique to the removed guard."""
    import inspect

    source = inspect.getsource(deploy.sync_env)
    assert "a DB model override" not in source
    assert "_resolved_slot_models" not in source


@pytest.mark.parametrize("model_var", ["GEMINI_MODEL", "GROQ_MODEL", "VERTEX_MODEL"])
def test_sync_env_warns_but_proceeds_past_an_unpriced_model(
    sync_ready, monkeypatch, capsys, model_var
):
    """An unpriced model no longer crashes anything -- estimate_cost_usd()
    returns None for it (spec section 6a) -- so this is now a non-blocking
    warning; the push proceeds past it. Returning None from find_service_id
    stops the run one guard later, at "no such service", which proves it got
    PAST the pricing guard without needing a full push."""
    monkeypatch.setattr(settings, model_var.lower(), "totally-made-up-model")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    code = deploy.sync_env()
    assert code == 1                       # "no Render service", not "refused"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert model_var in err
    assert "totally-made-up-model" in err
    assert "no Render service named" in err


def test_sync_env_warns_on_a_non_active_providers_unpriced_model(
    sync_ready, monkeypatch, capsys
):
    """sync_ready selects groq, whose own model is fine -- but VERTEX_MODEL is
    pushed by _wanted_env() too, and a DB provider flip can activate vertex
    with no redeploy. An unpriced value there warns exactly as the active
    provider's would; the warning must name vertex specifically."""
    monkeypatch.setattr(settings, "vertex_model", "totally-made-up-model")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    assert deploy.sync_env() == 1
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "VERTEX_MODEL" in err
    assert "vertex" in err


def test_sync_env_no_longer_refuses_an_unpriced_model(monkeypatch):
    """The old exit-2 existed only because estimate_cost_usd raised. It does
    not any more (spec section 6a), so the guard is now a warning.

    Asserted against the source because sync_env's other pre-push guards make
    a behavioural test require mocking the whole Render API surface. The
    matched fragment is the f-string literal unique to the removed refusal."""
    import inspect

    monkeypatch.setattr(settings, "groq_model", "llama-3.1-8b-instant")
    assert deploy._unpriced_models(), "precondition: the model must be unpriced"
    source = inspect.getsource(deploy.sync_env)
    assert "refusing to sync: {model_var}" not in source
    assert "warning: {model_var}" in source


def test_sync_env_allows_a_priced_model(sync_ready, monkeypatch, capsys):
    """The guard must not false-positive on the shipped defaults. Returning
    None from find_service_id stops the run at "no such service" -- which
    proves it got PAST the pricing guard without needing a full push."""
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    assert deploy.sync_env() == 1
    assert "no Render service named" in capsys.readouterr().err


def test_checks_registry_matches_what_run_checks_actually_runs(monkeypatch):
    """The registry is the single source: if run_checks stops consuming it,
    the generated checks.md silently starts describing a different tool."""
    names = [spec.name for spec in deploy.CHECKS]
    assert names == [
        "config", "pricing", "boot-creds-live", "github-app", "health",
        "database", "runtime-config", "provider", "provider-live",
        "api-key-live", "render-service", "uptime-pinger",
    ]
    _stub_all_checks(monkeypatch, ["PASS"] * 12)
    results = deploy.run_checks(frozenset(), "https://example.invalid")
    assert [r.name for r in results] == names


def test_every_check_spec_is_documented_and_well_formed():
    for spec in deploy.CHECKS:
        assert spec.verifies.strip(), f"{spec.name} has no description to generate from"
        assert callable(spec.func)
        assert isinstance(spec.required, bool)
        assert set(spec.args) <= {"repos", "base"}, f"{spec.name} wants an unknown argument"


def test_required_checks_are_the_ones_that_need_no_operator_key():
    """`required` drives the generated table's "Always runs?" column, so it has
    to mean something precise: a check that needs no operator-local key.
    RENDER_API_KEY and UPTIMEROBOT_API_KEY are now mandatory, so every check
    they used to gate is `required` too -- only DATABASE_URL still leaves a
    check optional (`database`, `provider`).

    Deliberately NOT "can fail the run" -- `pricing` always runs but only ever
    WARNs, so conflating the two would misdescribe it in the published table.
    """
    required = {spec.name for spec in deploy.CHECKS if spec.required}
    assert required == {
        "config", "pricing", "github-app", "health",
        "boot-creds-live", "provider-live", "api-key-live",
        "render-service", "uptime-pinger",
    }
