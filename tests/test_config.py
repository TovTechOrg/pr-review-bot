"""Settings validation: catches a misconfigured .env at startup rather than
silently reverting to unbounded/disabled behavior at runtime.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import OPERATIONAL_KEYS, Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z_0-9]+)=")


def _key_names(path: Path) -> set[str]:
    """Env-var NAMES only -- values are discarded before returning.

    This function is the reason a test may look at .env at all: it can only
    ever produce names, so no assertion built on it can print a secret. See
    CLAUDE.md's "Secret handling" section.
    """
    if not path.is_file():
        return set()
    return {
        match.group(1)
        for line in path.read_text().splitlines()
        if (match := _KEY_RE.match(line))
    }


def test_notice_sweep_batch_size_rejects_non_positive_values():
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(dispatcher_notice_sweep_batch_size=bad)


def test_notice_sweep_batch_size_accepts_positive_values():
    assert Settings(dispatcher_notice_sweep_batch_size=1).dispatcher_notice_sweep_batch_size == 1


def test_cooldown_factor_rejects_below_one():
    with pytest.raises(ValidationError):
        Settings(dispatcher_rereview_cooldown_factor=0.5)


def test_cooldown_factor_accepts_exactly_one():
    settings = Settings(dispatcher_rereview_cooldown_factor=1.0)
    assert settings.dispatcher_rereview_cooldown_factor == 1.0


def test_cooldown_factor_defaults_to_two():
    assert Settings().dispatcher_rereview_cooldown_factor == 2.0


def test_vertex_settings_default_to_derive_everything_from_the_key(monkeypatch):
    """VERTEX_GCP_PROJECT is an OPTIONAL override: unset means "use the project_id
    embedded in the service-account key itself" (design doc §2).

    _env_file=None plus delenv because these defaults must be asserted against
    the code, not against whatever this working copy's .env or the developer's
    exported shell happens to say."""
    for name in ("VERTEX_GCP_PROJECT", "VERTEX_GCP_LOCATION", "VERTEX_GCP_SERVICE_ACCOUNT_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.vertex_gcp_project == ""
    assert settings.vertex_gcp_location == "us-central1"
    assert settings.vertex_gcp_service_account_key == ""


def test_dashboard_credential_fields_default_to_empty_and_are_not_operational(monkeypatch):
    """_env_file=None plus delenv because these defaults must be asserted
    against the code, not against whatever this working copy's .env happens
    to say."""
    for name in ("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD", "DASHBOARD_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.dashboard_username == ""
    assert settings.dashboard_password == ""
    assert settings.dashboard_session_secret == ""
    assert "DASHBOARD_USERNAME" not in OPERATIONAL_KEYS
    assert "DASHBOARD_PASSWORD" not in OPERATIONAL_KEYS
    assert "DASHBOARD_SESSION_SECRET" not in OPERATIONAL_KEYS


def test_key_usage_caps_default_to_off(monkeypatch):
    """The cap defaults to None so an existing deployment that sets no env var
    sees no behavior change (design doc §2.1). _env_file=None plus delenv
    because these defaults must be asserted against the code, not against
    whatever this working copy's .env happens to say."""
    for name in (
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_RESET_TIME_UTC",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.key_usage_token_cap is None
    assert settings.key_usage_reset_time_utc == time(4, 0)


def test_key_usage_reset_time_parses_hh_mm(monkeypatch):
    """Arbitrary wall-clock granularity, not whole hours only -- a demo run
    sets the reset a couple of minutes out rather than waiting for the next
    hour boundary (design doc §2.1)."""
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "04:07")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(4, 7)


def test_key_usage_reset_time_parses_hh_mm_ss(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "23:59:30")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(23, 59, 30)


def test_key_usage_caps_parse_from_env(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_TOKEN_CAP", "20000")
    settings = Settings(_env_file=None)
    assert settings.key_usage_token_cap == 20000


def test_key_usage_reset_time_rejects_garbage(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "not-a-time")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_key_usage_caps_reject_non_positive_values():
    """0 (or negative) would make the dispatcher's `tokens >= 0` comparison
    unconditionally true -- every ticket deferred forever, and the deferral is
    STICKY (fixing the env var doesn't release already-deferred tickets). The
    cap must reject non-positive values at startup."""
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(key_usage_token_cap=bad)


def test_key_usage_caps_accept_positive_values():
    settings = Settings(key_usage_token_cap=1)
    assert settings.key_usage_token_cap == 1


def test_blank_env_value_falls_back_to_default_for_int_field(monkeypatch):
    """Regression: a key present in .env with nothing after the `=` (e.g. the
    template's unfilled `GITHUB_APP_INSTALLATION_ID=` line, which is exactly
    how a user following the setup guide's Step 2 hits this before Step 3)
    reaches pydantic as the literal string "" -- which used to fail int
    coercion and raise ValidationError at import, rather than falling back to
    the default the way a fully absent var already does."""
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "")
    assert Settings(_env_file=None).github_app_installation_id == 0


def test_blank_env_value_falls_back_to_default_for_time_field(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(4, 0)


def test_blank_env_value_does_not_mask_a_real_value(monkeypatch):
    """The blank-value drop must only catch genuinely empty values -- a real
    value set alongside a blank one elsewhere must still come through."""
    monkeypatch.setenv("GITHUB_APP_ID", "42")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "")
    settings = Settings(_env_file=None)
    assert settings.github_app_id == 42
    assert settings.github_app_installation_id == 0


def test_importing_config_with_installation_id_blank_in_env_file_does_not_raise(tmp_path):
    """The exact real-world reproduction: an .env file (not just a process env
    var) with the template's unfilled line still present. This is what a
    fresh `cp .env.example .env` followed by Step 2 (App ID, webhook secret,
    private key set; installation ID not yet known) leaves behind."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_APP_ID=42\nGITHUB_APP_INSTALLATION_ID=\n")
    settings = Settings(_env_file=(str(env),))
    assert settings.github_app_id == 42
    assert settings.github_app_installation_id == 0


def test_env_config_wins_over_env(tmp_path):
    """.env.config is the designated home for operational config, so it must
    win if a key somehow appears in both files."""
    env = tmp_path / ".env"
    env.write_text("GEMINI_MODEL=from_secrets_file\n")
    config = tmp_path / ".env.config"
    config.write_text("GEMINI_MODEL=from_config_file\n")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.gemini_model == "from_config_file"


def test_both_files_merge(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=sentinel-key\n")
    config = tmp_path / ".env.config"
    config.write_text("GEMINI_MODEL=groq\n")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.gemini_api_key == "sentinel-key"
    assert settings.gemini_model == "groq"


def test_process_env_beats_both_files(tmp_path, monkeypatch):
    """This is what makes Render unaffected by the split: neither file exists
    in the container, and injected env vars outrank both anyway."""
    env = tmp_path / ".env"
    env.write_text("GEMINI_MODEL=from_secrets_file\n")
    config = tmp_path / ".env.config"
    config.write_text("GEMINI_MODEL=from_config_file\n")
    monkeypatch.setenv("GEMINI_MODEL", "from_process_env")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.gemini_model == "from_process_env"


def test_every_operational_key_is_a_real_settings_field():
    """A typo in the allowlist would classify a key that cannot be read,
    silently exempting a real key from the placement guard below."""
    fields = set(Settings.model_fields)
    unknown = {key for key in OPERATIONAL_KEYS if key.lower() not in fields}
    assert not unknown, f"OPERATIONAL_KEYS names no such Settings field: {sorted(unknown)}"


def test_no_operational_key_lives_in_the_secrets_file():
    """Operational config must not sit in .env, because an agent may never open
    .env -- which is the entire point of the split. Reports NAMES only."""
    misplaced = _key_names(_REPO_ROOT / ".env") & OPERATIONAL_KEYS
    assert not misplaced, (
        f"move these keys from .env to .env.config: {sorted(misplaced)}"
    )


def test_no_unlisted_key_lives_in_the_config_file():
    """Secret-by-default: anything not on the allowlist must stay in .env."""
    intruders = _key_names(_REPO_ROOT / ".env.config") - OPERATIONAL_KEYS
    assert not intruders, (
        f"these keys are not on OPERATIONAL_KEYS and must live in .env: {sorted(intruders)}"
    )


_RETIRED_CREDENTIAL_KEYS = frozenset(
    {
        "GITHUB_APP_PRIVATE_KEY_B64",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GCP_SERVICE_ACCOUNT_KEY_B64",
        "GCP_SERVICE_ACCOUNT_KEY_PATH",
        # Retired by the 2026-09-08 env-var rename (VERTEX_ prefix grouping).
        # A leftover line here is silently IGNORED (Settings uses
        # extra="ignore"), so an operator who never renamed it would believe
        # vertex is configured when it is not.
        "GCP_SERVICE_ACCOUNT_KEY",
    }
)
_RETIRED_NUMBERED_RE = re.compile(
    r"^(GCP_SERVICE_ACCOUNT_KEY|GCP_SERVICE_ACCOUNT_KEY_B64|GCP_SERVICE_ACCOUNT_KEY_PATH)_\d+$"
)


def test_no_legacy_credential_var_lives_in_the_secrets_file():
    """Migration checklist for the verbatim-only credential convention
    (docs/superpowers/specs/2026-08-16-credential-convention-design.md) and
    the 2026-09-08 env-var rename: these names, and vertex's numbered
    _B64_n/_PATH_n/_n siblings, are retired and no Settings field reads them
    anymore. Reports NAMES only -- see CLAUDE.md's "Secret handling"
    section."""
    names = _key_names(_REPO_ROOT / ".env")
    legacy = {
        name
        for name in names
        if name in _RETIRED_CREDENTIAL_KEYS or _RETIRED_NUMBERED_RE.match(name)
    }
    assert not legacy, (
        f"retired credential var(s) still in .env, no longer read: {sorted(legacy)} -- "
        "rename GITHUB_APP_PRIVATE_KEY_B64 to GITHUB_APP_PRIVATE_KEY, "
        "GCP_SERVICE_ACCOUNT_KEY_B64[_n] or bare GCP_SERVICE_ACCOUNT_KEY[_n] to "
        "VERTEX_GCP_SERVICE_ACCOUNT_KEY[_n] (base64-encode any local key file first with "
        "scripts/encode_credential.py), and remove the _PATH variants entirely"
    )


def test_vertex_model_defaults_to_the_confirmed_working_vertex_model(monkeypatch):
    """gemini-flash-latest 404s on Vertex; gemini-2.5-flash is the value this
    project confirmed live (ISSUES.md). A non-empty default also keeps
    --sync-env's empty-value guard from ever tripping on it."""
    monkeypatch.delenv("VERTEX_MODEL", raising=False)
    assert Settings(_env_file=None).vertex_model == "gemini-2.5-flash"


def test_target_repos_splits_comma_separated_list():
    settings = Settings(github_target_repo="org/repo-a,org/repo-b", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo-a", "org/repo-b"})


def test_target_repos_strips_whitespace_around_entries():
    settings = Settings(github_target_repo=" org/repo-a , org/repo-b ", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo-a", "org/repo-b"})


def test_target_repos_star_means_no_restriction():
    settings = Settings(github_target_repo="*", _env_file=None)
    assert settings.target_repos() == frozenset()


def test_target_repos_empty_string_also_means_no_restriction():
    """A left-over empty value still parses without crashing, but it's no
    longer a valid *configured* state -- main.py's lifespan and
    scripts/deploy.py's check_config() both refuse to start/deploy on it,
    forcing an operator to pick "*" or a real allowlist explicitly."""
    settings = Settings(github_target_repo="", _env_file=None)
    assert settings.target_repos() == frozenset()


def test_is_target_repo_configured():
    assert Settings(github_target_repo="", _env_file=None).is_target_repo_configured() is False
    assert Settings(github_target_repo="*", _env_file=None).is_target_repo_configured() is True
    assert (
        Settings(github_target_repo="org/repo", _env_file=None).is_target_repo_configured()
        is True
    )


def test_target_repos_single_value_has_no_comma():
    settings = Settings(github_target_repo="org/repo", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo"})


def test_default_target_repo_is_the_only_value_when_single():
    settings = Settings(github_target_repo="org/repo", _env_file=None)
    assert settings.default_target_repo() == "org/repo"


def test_default_target_repo_is_the_first_of_several():
    """Regression test: a demo script (seed_demo_pr.py)
    reading github_target_repo directly under multi-repo config gets the
    whole comma-joined string, not a single valid repo -- default_target_repo()
    exists so those scripts get one deterministic repo instead."""
    settings = Settings(github_target_repo="org/repo-a,org/repo-b", _env_file=None)
    assert settings.default_target_repo() == "org/repo-a"


def test_default_target_repo_strips_whitespace():
    settings = Settings(github_target_repo=" org/repo-a , org/repo-b ", _env_file=None)
    assert settings.default_target_repo() == "org/repo-a"


def test_default_target_repo_empty_when_unset():
    settings = Settings(github_target_repo="", _env_file=None)
    assert settings.default_target_repo() == ""


def test_default_target_repo_empty_for_star():
    """"*" has no single repo to hand back -- a demo script needs a real
    testbed repo configured, not "act on everything"."""
    settings = Settings(github_target_repo="*", _env_file=None)
    assert settings.default_target_repo() == ""


def test_cost_cap_is_gone_entirely():
    """A dollar cap built on unverified rates is a safety control that can fail
    open -- worse than no cap (design spec 2026-08-18 section 6c)."""
    assert "KEY_USAGE_COST_CAP_USD" not in OPERATIONAL_KEYS
    assert not hasattr(Settings(_env_file=None), "key_usage_cost_cap_usd")


def test_importing_config_with_a_malformed_value_does_not_raise(tmp_path):
    """Regression: scripts/init_env.py imports Settings (the class) and
    OPERATIONAL_KEYS, never touching the `settings` singleton itself -- but
    config.py used to build that singleton unconditionally at import
    time regardless of what any caller actually needed, so a single
    already-malformed value left over in .env/.env.config (e.g. from before
    init_env's own answer-validation existed) crashed the bare import with a
    raw traceback, before init_env's own code could even run.

    A real subprocess with no .env/.env.config in its cwd, so the injected
    process env var is the only source for this key -- pydantic-settings
    validates a value from either source identically, so this exercises the
    same code path a bad line in .env.config would.
    """
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "KEY_USAGE_RESET_TIME_UTC": "4:00"}
    result = subprocess.run(
        [sys.executable, "-c", "from config import Settings, OPERATIONAL_KEYS; print('ok')"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_first_real_access_to_settings_still_raises_on_a_malformed_value(tmp_path):
    """The lazy singleton changes WHEN a genuinely invalid value is caught,
    never WHETHER it is -- the app's own boot path (main.py) still reads
    `settings` immediately on startup, so a real misconfiguration must still
    fail loudly the moment anything actually asks for it."""
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "KEY_USAGE_RESET_TIME_UTC": "4:00"}
    result = subprocess.run(
        [sys.executable, "-c", "import config; config.settings"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "ValidationError" in result.stderr
