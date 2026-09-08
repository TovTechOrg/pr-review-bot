"""Deploy verification CLI for the hosted Render + Supabase deployment.

Runs twelve independent checks and prints one aligned table. Every check runs
regardless of earlier failures, so a single run surfaces every problem rather
than only the first. Exit codes: 0 all ok, 1 at least one check failed, 2 the
CLI could not run at all.

Standalone by design: nothing here assumes Claude Code, an assistant, or an
interactive terminal. `.claude/commands/deploy.md` is a convenience wrapper
that holds no logic.

Output is terse by contract (design spec section 7.4): details are fragments
naming the observed fact and the next action, never the reasoning -- the
explanations live in the published guide (see _GUIDE_URL below).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, NamedTuple
from urllib.parse import urlsplit

import httpx
import psycopg
from github import GithubException

import github_app
import render_client as _render
from config import settings
from providers import pricing, registry
from review_queue import store
from scripts import _override
from scripts._prereqs import _looks_like_local_test_db

_NAME_WIDTH = 18
_STATUS_WIDTH = 9
_GUIDE_BASE = "https://tovtechorg.github.io/pr-review-bot"
_GUIDE_URL = f"{_GUIDE_BASE}/operations/deploy/"
_HTTP_TIMEOUT = 10.0
_DB_CONNECT_TIMEOUT = 10
_UPTIMEROBOT_API = "https://api.uptimerobot.com/v2/getMonitors"
# Render free instances spin down after ~15 minutes idle; 10 minutes leaves margin.
_MAX_PINGER_INTERVAL_SECONDS = 600

# The service env vars --sync-env always pushes, regardless of provider.
# Authoritative: scripts/gen_docs.py renders this list (plus every
# credential/model var named in _PROVIDERS) straight into
# guide/reference/sync-env.md, so the published guide can never drift from
# what this tuple actually contains.
_ALWAYS_SYNCED = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_SECRET",
    # The deployed service now needs its own RENDER_API_KEY at runtime for
    # dashboard/environment.py's Environment tab (docs/superpowers/specs/
    # 2026-09-02-dashboard-environment-tab-design.md) -- previously only the
    # separate onboarding-wizard project's bulk push included it, so a
    # service provisioned via this CLI path instead of the wizard was
    # silently missing it, with no check catching the gap.
    "RENDER_API_KEY",
)
# VERTEX_GCP_PROJECT unset means "use the project_id embedded in the service-account
# key" (see config.py), not a missing value -- exempt from sync_env()'s
# "refuse to push empty values" guard below. GITHUB_TARGET_REPO used to be
# exempt too (empty meant "track all repos"), but that sentinel is now the
# explicit, non-empty "*" (docs/superpowers/specs/2026-08-17-multi-repo-
# support-design.md's track-all mode, updated 2026-09-07): a genuinely empty
# GITHUB_TARGET_REPO is unconfigured, not deliberate, so it goes through the
# ordinary empty-value refusal like every other required key.
_OPTIONAL_EMPTY_ENV_KEYS = frozenset({"VERTEX_GCP_PROJECT"})

# OPERATIONAL_KEYS (config.py) names, mapped to the Settings attribute
# holding their local value, for every one that has NO other sync path here:
# not LLM_PROVIDER/GITHUB_TARGET_REPO (handled directly in _wanted_env()
# below), not a provider credential/model var (handled via _PROVIDERS), not a
# _DB_SYNCED_OPERATIONAL_KEYS or _NEVER_SYNCED_OPERATIONAL_KEYS name (both
# below). Before this existed, editing any of these in .env.config and
# running --sync-env silently pushed nothing -- see ISSUES.md's 2026-08-17
# "--sync-env silently never pushes 12 of the documented operational env
# vars" entry.
_GENERIC_OPERATIONAL_ENV_ATTRS = {
    "VERTEX_GCP_PROJECT": "vertex_gcp_project",
    "VERTEX_GCP_LOCATION": "vertex_gcp_location",
    "LLM_REQUEST_TIMEOUT_SECONDS": "llm_request_timeout_seconds",
    "DISPATCHER_IDLE_SLEEP_SECONDS": "dispatcher_idle_sleep_seconds",
    "DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS": "dispatcher_default_retry_after_seconds",
    "DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS": "dispatcher_failure_base_backoff_seconds",
    "DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS": "dispatcher_failure_max_backoff_seconds",
    "DISPATCHER_MAX_FAILURE_ATTEMPTS": "dispatcher_max_failure_attempts",
    "DISPATCHER_MAX_NOTICE_POST_ATTEMPTS": "dispatcher_max_notice_post_attempts",
    "DISPATCHER_MIN_RETRY_AFTER_SECONDS": "dispatcher_min_retry_after_seconds",
    "DISPATCHER_BACKOFF_JITTER_SECONDS": "dispatcher_backoff_jitter_seconds",
    "DISPATCHER_NOTICE_SWEEP_BATCH_SIZE": "dispatcher_notice_sweep_batch_size",
}

# These 6 have their own DB-backed live-override mechanism (runtime_config,
# via cooldown_config.py/usage_cap_config.py/review_draft_config.py) --
# unlike every other operational key, they are never a Render env var at all
# (see render.yaml and the 2026-08-17 "two sources of truth" design note):
# --sync-config-db (and --sync-env, which calls the same push) mirrors
# .env.config straight into runtime_config instead, which is what the app
# actually reads.
_DB_SYNCED_OPERATIONAL_KEYS = frozenset(
    {
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_RESET_TIME_UTC",
        "DISPATCHER_REREVIEW_COOLDOWN_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_FACTOR",
        "REVIEW_DRAFT_PRS",
    }
)

# Operator-machine-only settings: which Render service to check/deploy, and
# what public URL to hit. config.py's own field comments say these must
# NEVER be set on the deployed service itself -- pushing them would just
# create dead env vars, not fix anything.
_NEVER_SYNCED_OPERATIONAL_KEYS = frozenset({"RENDER_SERVICE_NAME", "PUBLIC_BASE_URL"})

_DEPLOY_POLL_SECONDS = 10
# A cold Docker build with a full dependency install runs well past five
# minutes; the measured ~60s redeploys had warm layers. Too short a timeout
# makes the FIRST deploy the most likely to report a false failure.
_DEPLOY_TIMEOUT_SECONDS = 900
_DEPLOY_IN_FLIGHT_STATUSES = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
# "canceled" is deliberately absent: it is what a superseding deploy looks
# like, not a build failure, and is reported separately.
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "deactivated",
}
# ~5 minutes between progress lines at the default 10s poll interval, so a
# long in-flight wait never looks hung -- there is always visible output
# within a few minutes, not total silence until the final result.
_IN_FLIGHT_PROGRESS_EVERY = 30


@dataclass(frozen=True)
class CheckResult:
    """One row of the report. ``detail`` is the whole user experience for a
    failing line: it must name what is wrong and what to do, because a terminal
    user has nothing else to work from. A newline in ``detail`` renders as an
    indented continuation line, used only to enumerate observed values."""

    name: str
    status: Literal["PASS", "WARN", "FAIL", "SKIPPED"]
    detail: str = ""


def resolve_base_url() -> str:
    """This deployment's public origin, normalized exactly once.

    The rstrip is not cosmetic: check_uptime_pinger compares the monitor's URL
    by exact equality, so a trailing slash here would produce a doubled slash
    and fail a correctly configured pinger.
    """
    base = settings.public_base_url or os.environ.get("RENDER_EXTERNAL_URL", "")
    return base.rstrip("/")


# Single source of truth for provider -> env-var-name mappings, shared with
# the root-level review engine -- see providers/registry.py. _PROVIDERS is kept as a
# module-level alias so every existing call site in this file (and in
# scripts/set_override.py) keeps working unchanged.
_PROVIDERS = registry.PROVIDERS


def _unpriced_models(
    overrides: dict[str, str | None] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Every provider whose EFFECTIVE model has no rate-table entry, as
    (provider, model_var, model, known-models string).

    Checked for EVERY provider, not just the active one -- exactly as
    _wanted_env() pushes every provider's model var: a DB provider override
    can activate any of them with no redeploy, so an unpriced value sitting in
    a currently-inactive provider's var is a live landmine, not a harmless one.

    `overrides`, when given, is a {provider: DB model override or None} map
    (as returned by _resolved_model_overrides()) -- the EFFECTIVE model (an
    active override, else the local value) is what gets checked, so an
    override set past set_override.py's own warning is reported too.
    check_pricing() passes this (it must report what will actually run);
    sync_env() omits it (it is warning about a PUSH of the local value -- its
    own model-override-disagreement guard already refuses when an active
    override differs from the local value about to be pushed, and when
    they're equal, the plain local-value check below catches the unpriced
    case).

    An empty model is skipped deliberately: that is a distinct, pre-existing
    failure mode, and piling a second, confusing message onto it adds noise
    rather than clarity. In practice it never fires -- every Settings model
    field carries a non-empty, priced default.

    Shared by check_pricing() (which reports all of them as one WARN row)
    and sync_env() (which prints one warning line each), so the two can never
    disagree about what counts as unpriced. Neither blocks: an unpriced model
    runs, it just carries no cost estimate (design spec 2026-08-18 section 6b).
    """
    overrides = overrides or {}
    unpriced: list[tuple[str, str, str, str]] = []
    for provider, (_credential, model_var) in sorted(_PROVIDERS.items()):
        local_model = getattr(settings, model_var.lower(), "")
        model = overrides.get(provider) or local_model
        if model and not pricing.is_known(provider, model):
            known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
            unpriced.append((provider, model_var, model, known))
    return unpriced


def check_config() -> CheckResult:
    """Every value the deployed service needs, resolvable locally.

    Reports missing key NAMES only -- never a secret value, never a length;
    non-secret values (provider, model) are named deliberately."""
    missing: list[str] = []
    problems: list[str] = []
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    if not settings.github_app_private_key:
        missing.append("GITHUB_APP_PRIVATE_KEY")
    if not settings.github_app_installation_id:
        missing.append("GITHUB_APP_INSTALLATION_ID")
    if not settings.github_webhook_secret:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not settings.github_target_repo:
        missing.append("GITHUB_TARGET_REPO")
    if not settings.dashboard_username:
        missing.append("DASHBOARD_USERNAME")
    if not settings.dashboard_password:
        missing.append("DASHBOARD_PASSWORD")
    if not settings.dashboard_session_secret:
        missing.append("DASHBOARD_SESSION_SECRET")
    elif len(settings.dashboard_session_secret) < 32:
        problems.append(
            "DASHBOARD_SESSION_SECRET is too short to safely sign session tokens "
            "(must be at least 32 characters) -- a short HS256 key is brute-forceable "
            'offline by anyone who captures one session cookie. Generate one with: '
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not resolve_base_url():
        missing.append("PUBLIC_BASE_URL or RENDER_EXTERNAL_URL")
    if not settings.llm_provider:
        problems.append(
            "LLM_PROVIDER is unset -- there is no default. Set it in .env.config "
            f"to one of: {', '.join(sorted(_PROVIDERS))}"
        )
    elif (entry := _PROVIDERS.get(settings.llm_provider)) is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        problems.append(
            f"LLM_PROVIDER={settings.llm_provider!r} is not supported "
            f"(expected one of: {accepted})"
        )
    else:
        credential = entry[0]
        if not getattr(settings, credential.lower(), ""):
            missing.append(credential)

    detail_lines = []
    if missing:
        detail_lines.append("missing: " + ", ".join(missing))
    detail_lines.extend(problems)
    if detail_lines:
        return CheckResult("config", "FAIL", "\n".join(detail_lines))
    return CheckResult("config", "PASS", "")


def check_pricing() -> CheckResult:
    """Whether every provider's effective model has a rate-table entry.

    WARN, never FAIL: an unpriced model runs fine, it simply produces no cost
    estimate on the comment (providers/pricing.py::estimate_cost_usd
    returns None). This used to be folded into check_config as a FAIL, back
    when an unpriced model crashed the review after three paid calls
    (design spec 2026-08-18 section 6b).

    `model` (from _unpriced_models()) is the EFFECTIVE value -- an active DB
    override when one is set, else the local env var -- but the env var's
    NAME (model_var) is always available regardless of which one is in
    effect. Naming model_var as the source when an override is actually what
    supplied the unpriced value would misleadingly blame an env var that may
    hold something else entirely and isn't even being read; branch on
    whether an override is active so the message always names the real
    source, matching the override-branching message this file used to build
    inside check_config() before check_pricing() was extracted out of it.
    """
    overrides: dict[str, str | None] = {}
    if settings.database_url:
        try:
            overrides = _resolved_model_overrides()
        except Exception:  # noqa: BLE001
            overrides = {}
    lines = []
    for provider, model_var, model, known in _unpriced_models(overrides):
        if overrides.get(provider):
            lines.append(
                f"{provider} model override {model!r} has no pricing-table entry "
                f"(known {provider} models: {known}); {model_var} is not consulted "
                "while this override is active -- reviews run, with no cost "
                "estimate. Clear it or add a pricing.py entry: uv run python -m "
                f"scripts.set_override {provider} --clear-model --no-activate"
            )
        else:
            lines.append(
                f"{model_var}={model!r} has no pricing-table entry for {provider} "
                f"(known: {known}) -- reviews run, with no cost estimate"
            )
    if lines:
        return CheckResult("pricing", "WARN", "\n".join(lines))
    return CheckResult("pricing", "PASS", "")


# The vars main.py's lifespan touches unconditionally at every boot --
# LLM_PROVIDER (must be a supported provider) and GITHUB_WEBHOOK_SECRET
# (must be non-empty) checked directly; GITHUB_APP_INSTALLATION_ID (must be
# non-empty) then re-verified against GitHub via discover_and_verify_
# installation_id(), which itself needs GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY
# to make that call; DATABASE_URL via init_pool(). Unlike an earlier version
# of this list, GITHUB_APP_INSTALLATION_ID's discovery is never skipped
# because it's "already set" -- main.py refuses to start at all if it's
# unset (ISSUES.md 2026-08-21), so it's just as boot-critical as the other
# eight (the three DASHBOARD_* vars included -- main.py's lifespan
# refuses to start with any of them empty or too short too). A rename/drop
# of any of these nine that never reached Render crashes the whole ASGI app
# at startup, not just one feature.
_BOOT_CREDENTIAL_NAMES = (
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "LLM_PROVIDER",
    "DATABASE_URL",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_SECRET",
)


def check_boot_credentials_live() -> CheckResult:
    """Whether the vars the service needs at every boot are genuinely present
    on the live Render service -- not just locally.

    `config` validates the local `.env`; this is the check that would have
    caught the GITHUB_APP_PRIVATE_KEY_B64 -> GITHUB_APP_PRIVATE_KEY rename
    that crashed a live deploy (Render still had the old name, the new code
    read the new one, got an empty string, and PyGithub's own assertion
    crashed the whole app during startup).
    """
    name = "boot-creds-live"
    if not settings.render_api_key:
        return CheckResult(
            name, "FAIL", "RENDER_API_KEY is required -- set it to verify credentials "
            "against the live service"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        live = _render.env_vars(service_id)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    missing = [key for key in _BOOT_CREDENTIAL_NAMES if not live.get(key)]
    if missing:
        return CheckResult(name, "FAIL", "not present on Render: " + ", ".join(missing))
    return CheckResult(name, "PASS", "present on Render: " + ", ".join(_BOOT_CREDENTIAL_NAMES))


def check_installation_and_webhook(repos: frozenset[str], base: str) -> CheckResult:
    """Installation discovery, GITHUB_APP_INSTALLATION_ID verification,
    allowlist verification, plus an idempotent webhook registration.

    Resolves the installation id at the App level (github_app.
    discover_installation_id_for_app) -- this project's scope is one App
    installation per account/org, so no specific repo is needed to seed the
    lookup (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md).
    GITHUB_APP_INSTALLATION_ID is required and never guessed on the
    operator's behalf (ISSUES.md 2026-08-21): a FAIL here if it's unset, or
    if it disagrees with the id just discovered (most likely because the
    App was uninstalled and reinstalled, which GitHub assigns a new id for).

    If `repos` (the GITHUB_TARGET_REPO allowlist) is non-empty, every entry is
    verified against the installation's actual repo list
    (github_app.list_installation_repos) -- an entry the installation does not
    cover is reported as a FAIL naming it: unlike a repo simply excluded from
    the allowlist (silently and correctly dropped by the webhook filter), a
    repo listed here but not installed never generates a webhook at all, so
    this check is the only place that misconfiguration is ever visible. If
    `repos` is empty (track-all mode), nothing is configured to verify, so the
    installation id and covered-repo count are reported as PASS.

    A FAIL here is deliberately not split into severities, even though it
    covers two functionally different situations: a genuine typo (the App was
    never installed on this repo) and a config-hygiene nit (it was installed
    and later removed via GitHub's own UI -- zero runtime risk, since GitHub
    simply stops delivering webhooks for a repo the App isn't installed on;
    see webhook.py's target_repos() filter and CLAUDE.md's process notes
    for the 2026-08-17 analysis). `list_installation_repos()` has no notion of
    "was covered, now isn't" versus "never was" -- GitHub's API cannot
    distinguish them server-side -- so a severity split here could only ever
    soften *both* cases at once, weakening the real-typo signal to avoid
    alarming on the harmless one. The detail message names both possible
    causes instead, so an operator reading a FAIL knows what to check before
    treating it as urgent.

    Reads the current webhook URL before writing so a re-run reports "already
    correct" rather than silently re-PATCHing, and so a failed read never
    triggers a blind write that could clobber a good URL.
    """
    name = "github-app"
    try:
        installation_id = github_app.discover_installation_id_for_app()
    except github_app.AppNotInstalledError:
        return CheckResult(name, "FAIL", "App not installed; install via GitHub UI")
    except RuntimeError as exc:
        status = getattr(exc.__cause__, "status", None)
        detail = "installation lookup failed; check App ID / private key"
        if status is not None:
            detail += f" ({status})"
        else:
            detail = str(exc)
        return CheckResult(name, "FAIL", detail)

    # GITHUB_APP_INSTALLATION_ID is required and never guessed on the
    # operator's behalf (ISSUES.md 2026-08-21) -- verified here against the
    # id just discovered, independent of check_config()'s own unset check,
    # since this function is also callable on its own.
    if not settings.github_app_installation_id:
        return CheckResult(
            name, "FAIL",
            f"GITHUB_APP_INSTALLATION_ID is unset (discovered installation="
            f"{installation_id}) -- this project requires it to be configured "
            "explicitly; set it to the value above.",
        )
    if settings.github_app_installation_id != installation_id:
        return CheckResult(
            name, "FAIL",
            f"GITHUB_APP_INSTALLATION_ID={settings.github_app_installation_id} does not "
            f"match the App's actual installation id={installation_id} -- the App was "
            "likely uninstalled and reinstalled; update GITHUB_APP_INSTALLATION_ID.",
        )

    try:
        covered = github_app.list_installation_repos(installation_id)
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; repo list failed ({exc.status})"
        )

    if repos:
        missing = github_app.repos_not_covered(covered, repos)
        if missing:
            return CheckResult(
                name, "FAIL",
                f"installation={installation_id}; not covered by the installation: "
                + ", ".join(missing)
                + "\neither a typo in GITHUB_TARGET_REPO, or the App was removed from "
                "this repo after it was added (GitHub can't tell the two apart) -- "
                "check the App's Installed repositories list on GitHub first",
            )
        repo_detail = f"installation={installation_id}; allowlist covered ({len(repos)} repo(s))"
    else:
        repo_detail = f"installation={installation_id}; tracking all {len(covered)} repo(s)"

    wanted = f"{base}/webhook"
    try:
        current = github_app.get_webhook_url()
    except GithubException as exc:
        return CheckResult(name, "FAIL", f"{repo_detail}; webhook read failed ({exc.status})")
    if current == wanted:
        return CheckResult(name, "PASS", f"{repo_detail}; webhook already correct")
    try:
        github_app.set_webhook_url(wanted)
    except GithubException as exc:
        return CheckResult(name, "FAIL", f"{repo_detail}; webhook write failed ({exc.status})")
    if current:
        return CheckResult(name, "PASS", f"{repo_detail}; webhook updated from {current}")
    return CheckResult(name, "PASS", f"{repo_detail}; webhook set")


def check_health_endpoint(base: str) -> CheckResult:
    """Both verbs must answer 200.

    HEAD is not redundant: UptimeRobot's free tier sends HEAD by default, so a
    GET-only /healthz returns 405 to the pinger and the instance sleeps -- a
    failure invisible from a browser.
    """
    name = "health"
    url = f"{base}/healthz"
    try:
        get_status = httpx.get(url, timeout=_HTTP_TIMEOUT).status_code
        head_status = httpx.head(url, timeout=_HTTP_TIMEOUT).status_code
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"{type(exc).__name__} reaching {url}")
    if get_status != 200 and head_status != 200:
        return CheckResult(name, "FAIL", f"GET -> {get_status}, HEAD -> {head_status}")
    if get_status != 200:
        return CheckResult(name, "FAIL", f"GET /healthz -> {get_status} (HEAD ok)")
    if head_status != 200:
        return CheckResult(
            name, "FAIL", f"HEAD /healthz -> {head_status} (GET ok); pinger sends HEAD"
        )
    return CheckResult(name, "PASS", "GET + HEAD -> 200")


def check_database() -> CheckResult:
    """Reachability AND provisioning, via a raw connection.

    Deliberately not store.init_pool(): the pool waits 30s before raising and
    its message is written for a startup log, not a checklist. A raw connect
    with a short timeout reports the driver's real failure in about a second.

    The failure detail names the exception type and the (non-secret) hostname
    only -- settings.database_url carries the password.
    """
    name = "database"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to check the queue database")
    host = urlsplit(settings.database_url).hostname or "?"
    try:
        with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
            conn.execute("SELECT 1")
            provisioned = conn.execute("SELECT to_regclass('public.tickets')").fetchone()[0]
    except psycopg.Error as exc:
        return CheckResult(name, "FAIL", f"cannot connect to {host} ({type(exc).__name__})")
    if provisioned is None:
        return CheckResult(name, "FAIL", "connected; tickets absent -- app never booted on this DB")
    return CheckResult(name, "PASS", "connected; tickets present")


def _missing_runtime_config_columns() -> list[str] | None:
    """Names from store.RUNTIME_CONFIG_COLUMNS absent from the live
    runtime_config table, in declared order -- or None if the table itself
    doesn't exist yet, a distinct situation from "every column is missing"
    (see check_runtime_config_schema).

    store.py's schema is declared, not migrated (CREATE TABLE IF NOT EXISTS
    is a no-op against a table that already exists), so a column added there
    after a database's runtime_config was first provisioned never reaches it
    on its own -- this is exactly the gap that left review_draft_prs missing
    against the real Render database and made sync_config_db() crash with a
    raw UndefinedColumn instead of naming the problem.

    Raises psycopg.Error on a connection failure -- callers already have
    their own way of reporting that.
    """
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute("SELECT to_regclass('public.runtime_config')").fetchone()
        if (row[0] if row else None) is None:
            return None
        live = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'runtime_config'"
            ).fetchall()
        }
    return [name for name, _ in store.RUNTIME_CONFIG_COLUMNS if name not in live]


def _runtime_config_alter_statements(missing: list[str]) -> str:
    """One `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` line per name in
    `missing`, in the type each is declared with in
    store.RUNTIME_CONFIG_COLUMNS -- the exact, ready-to-run fix for the
    no-migration-code gap _missing_runtime_config_columns() documents."""
    by_name = dict(store.RUNTIME_CONFIG_COLUMNS)
    return "\n".join(
        f"ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS {name} {by_name[name]};"
        for name in missing
    )


def check_runtime_config_schema() -> CheckResult:
    """Whether the live runtime_config table has every column store.py's
    schema declares -- catches a column added to RUNTIME_CONFIG_COLUMNS
    after a database's runtime_config was already provisioned, which the
    app's own CREATE TABLE IF NOT EXISTS boot DDL can never backfill (see
    _missing_runtime_config_columns's docstring). Without this, the first
    symptom is sync_config_db() crashing with a raw UndefinedColumn on
    whichever of these six columns it happens to write.
    """
    name = "runtime-config"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to check runtime_config's schema")
    try:
        missing = _missing_runtime_config_columns()
    except psycopg.Error as exc:
        return CheckResult(name, "FAIL", f"cannot check runtime_config ({type(exc).__name__})")
    problem = _runtime_config_schema_problem(missing)
    if problem:
        return CheckResult(name, "FAIL", problem)
    return CheckResult(name, "PASS", "all declared columns present")


def _runtime_config_schema_problem(missing: list[str] | None) -> str | None:
    """Detail message for `missing` (as returned by
    _missing_runtime_config_columns()), or None if there's nothing wrong.
    Shared by check_runtime_config_schema (wraps this in a CheckResult) and
    sync_config_db (prints it and refuses to write), so the two can never
    describe this gap differently."""
    if missing is None:
        return "runtime_config does not exist -- deploy once so boot DDL provisions it"
    if not missing:
        return None
    return (
        "missing column(s): " + ", ".join(missing) + "\nstore.py's schema is declared, not "
        "migrated, so these were never backfilled -- add them manually:\n"
        + _runtime_config_alter_statements(missing)
    )


def _resolved_provider() -> tuple[str, str | None]:
    """(active provider, override or None). The override wins at runtime, so
    the CLI must resolve exactly as the dispatcher does.

    Reads via a raw short-timeout connection rather than store.init_pool(),
    for the same reason check_database does: the pool blocks 30s before
    raising, which is a startup-log behaviour, not a checklist one -- and this
    is a one-shot CLI, not a long-lived service amortising that cost.

    A bare psycopg.connect (unlike the store's pool) does not set
    row_factory=dict_row, so the row -- if any -- comes back as a tuple, and
    an empty-string override normalizes to None exactly as
    store.get_provider_override() does, so the CLI and the dispatcher can
    never disagree about whether an override is active.
    """
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    override = (row[0] if row else None) or None
    return (override or settings.llm_provider), override


def _resolved_model_overrides() -> dict[str, str | None]:
    """{provider: DB model override or None} for every provider in
    registry.PROVIDERS, in ONE connection -- mirrors
    review_queue/store.py::get_all_model_overrides()'s single-query shape, via
    this file's raw short-timeout connection rather than the pool, for the
    same reason _resolved_provider does. Used by sync_env()'s model-override
    guard, which used to call a per-provider version of this (one connection
    per provider, in a loop) -- an unreachable DB then cost ~3x the connect
    timeout instead of 1x. Column names come from registry.MODEL_COLUMNS -- a
    hardcoded whitelist -- and are never built from a caller-supplied string.
    """
    columns = registry.MODEL_COLUMNS
    select = ", ".join(columns.values())
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute(f"SELECT {select} FROM runtime_config WHERE id = 1").fetchone()
    if row is None:
        return dict.fromkeys(columns, None)
    return {provider: (value or None) for provider, value in zip(columns, row)}


def check_provider() -> CheckResult:
    """Which provider will actually run, and whether its credential exists.

    Without this, a DB override makes every other check's provider assumption
    unverifiable: the service could run a provider whose key was never checked.
    """
    name = "provider"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to resolve the override")
    try:
        provider, override = _resolved_provider()
    # deliberate: a DB problem is database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIPPED", f"could not read the override ({type(exc).__name__})")
    source = f"DB override; env={settings.llm_provider}" if override else "env"
    entry = _PROVIDERS.get(provider)
    if entry is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) is not supported (expected: {accepted})"
        )
    credential = entry[0]
    if not getattr(settings, credential.lower(), ""):
        return CheckResult(name, "FAIL", f"{provider} ({source}) -- {credential} missing")
    return CheckResult(name, "PASS", f"{provider} ({source})")


def _resolved_provider_or_env() -> tuple[str, str | None]:
    """Like _resolved_provider(), but usable without DATABASE_URL: without a
    database there is no override to check, so this falls back to the
    env-configured provider instead of requiring a connection. Used by
    check_provider_live(), which -- unlike check_provider() -- must answer
    "what's actually running" even when there's no override to resolve.
    """
    if not settings.database_url:
        return settings.llm_provider, None
    return _resolved_provider()


def _resolved_key_index(provider: str) -> tuple[int, int | None]:
    """(active index, override or None) for `provider`. Reads via a raw
    short-timeout connection, mirroring _resolved_provider() for the same
    reason: a one-shot CLI must not pay store.init_pool()'s 30s timeout.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    override = row[0] if row else None
    return (override if override is not None else 0), override


def _resolved_key_index_or_env(provider: str) -> tuple[int, int | None]:
    """Like _resolved_key_index(), but usable without DATABASE_URL: without a
    database there is no override to check, so this falls back to index 0.
    """
    if not settings.database_url:
        return 0, None
    return _resolved_key_index(provider)


def check_provider_live() -> CheckResult:
    """Whether the actively-resolved provider's credential is genuinely
    present on the live Render service -- not just locally.

    `provider` validates the local `.env`; this is the check that would have
    caught the demo-rehearsal failure where a DB override named a provider
    whose key was never pushed to Render.
    """
    name = "provider-live"
    if not settings.render_api_key:
        return CheckResult(
            name, "FAIL", "RENDER_API_KEY is required -- set it to verify credentials "
            "against the live service"
        )
    try:
        provider, override = _resolved_provider_or_env()
    # deliberate: a DB problem is provider's/database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active provider ({type(exc).__name__})"
        )
    if override:
        source = f"DB override; env={settings.llm_provider}"
    elif not settings.database_url:
        source = "env; no DATABASE_URL to check for an override"
    else:
        source = "env"
    entry = _PROVIDERS.get(provider)
    if entry is None:
        # check_config / check_provider already FAIL on an unsupported name;
        # there is no credential key to look up without a table entry.
        return CheckResult(name, "SKIPPED", f"{provider} ({source}) is not a supported provider")
    credential = entry[0]
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        live_value = _render.env_vars(service_id).get(credential) or ""
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not live_value:
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) -- {credential} not present on Render"
        )
    return CheckResult(name, "PASS", f"{provider} ({source}) -- {credential} present on Render")


def check_api_key_live() -> CheckResult:
    """Whether the actively-resolved provider's actively-resolved key SLOT is
    genuinely present on the live Render service -- catches "the DB says
    index 2 but nobody ever pushed GROQ_API_KEY_2 to Render", the same class
    of gap check_provider_live catches for the provider name itself.
    """
    name = "api-key-live"
    if not settings.render_api_key:
        return CheckResult(
            name, "FAIL", "RENDER_API_KEY is required -- set it to verify credentials "
            "against the live service"
        )
    try:
        provider, _provider_override = _resolved_provider_or_env()
    # deliberate: a DB problem is provider's/database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active provider ({type(exc).__name__})"
        )
    entry = _PROVIDERS.get(provider)
    if entry is None:
        return CheckResult(name, "SKIPPED", f"{provider} is not a supported provider")
    try:
        index, index_override = _resolved_key_index_or_env(provider)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active key index ({type(exc).__name__})"
        )
    credential, _ = entry
    env_name = credential if index == 0 else f"{credential}_{index}"
    source = f"index {index}" + (" (DB override)" if index_override is not None else "")
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        live_value = _render.env_vars(service_id).get(env_name) or ""
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not live_value:
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) -- {env_name} not present on Render"
        )
    return CheckResult(name, "PASS", f"{provider} ({source}) -- {env_name} present on Render")


def _local_head() -> tuple[str, bool] | None:
    """(short HEAD sha, working tree is dirty), or None outside a git repo.

    Uses subprocess rather than a dependency: the CLI must run from a plain
    checkout with no extra installs.
    """
    import subprocess

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None
    return (head, dirty) if head else None


def check_render_service() -> CheckResult:
    """Why the service is or is not serving -- health already covers whether."""
    name = "render-service"
    if not settings.render_api_key:
        return CheckResult(
            name, "FAIL", "RENDER_API_KEY is required -- set it to check deploy status"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        resp = httpx.get(
            f"{_render.RENDER_API}/services/{service_id}/deploys",
            params={"limit": 1},
            headers=_render.headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        deploys = resp.json()
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not deploys:
        return CheckResult(name, "FAIL", "service exists but has no deploys")
    deploy_obj = _render.unwrap(deploys[0], "deploy")
    status = deploy_obj.get("status", "?")
    if status != "live":
        return CheckResult(name, "FAIL", f"latest deploy status: {status}")

    deploy_id = deploy_obj.get("id", "?")
    commit = (deploy_obj.get("commit") or {}).get("id") or ""
    image = (deploy_obj.get("image") or {}).get("ref") or ""

    if image:
        return CheckResult(
            name, "PASS",
            f"live: {deploy_id} @ {image}\n(image-backed; no local comparison possible)",
        )
    if not commit:
        # Assumption 4 is unverified -- degrade rather than invent a failure.
        return CheckResult(name, "PASS", f"live: {deploy_id}")

    local = _local_head()
    if local is None:
        return CheckResult(
            name, "PASS", f"live: {deploy_id} @ {commit[:7]} (no git checkout here)"
        )
    head, dirty = local
    # Compare on a common short prefix: Render returns a full sha, `git
    # rev-parse --short` a 7-char one, so a direct == would always differ.
    if commit[:7] != head[:7]:
        return CheckResult(
            name, "FAIL",
            f"live: {deploy_id} @ {commit[:7]}, but local HEAD is {head}\n"
            f"push, or re-run --sync-env, to deploy what you have",
        )
    if dirty:
        return CheckResult(
            name, "FAIL",
            f"live: {deploy_id} @ {commit[:7]} (local HEAD matches, tree dirty\n"
            f"-- uncommitted changes cannot be in any build)",
        )
    return CheckResult(name, "PASS", f"live: {deploy_id} @ {commit[:7]}")


def check_uptime_pinger(base: str) -> CheckResult:
    """The keep-warm monitor exists, is active, and polls often enough.

    The URL is compared by exact equality on purpose: the real outage was a
    trailing comma, which fired perfectly on schedule and 404'd every time.
    """
    name = "uptime-pinger"
    if not settings.uptimerobot_api_key:
        return CheckResult(
            name, "FAIL", "UPTIMEROBOT_API_KEY is required -- set it to check keep-warm"
        )
    wanted = f"{base}/healthz"
    try:
        resp = httpx.post(
            _UPTIMEROBOT_API,
            data={"api_key": settings.uptimerobot_api_key, "format": "json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        monitors = resp.json().get("monitors") or []
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"UptimeRobot API error ({type(exc).__name__})")

    match = next((m for m in monitors if m.get("url") == wanted), None)
    if match is None:
        found = ", ".join(m.get("url", "?") for m in monitors) or "none"
        return CheckResult(name, "FAIL", f"no monitor matches {wanted}\nfound: {found}")
    if match.get("status") == 0:
        return CheckResult(name, "FAIL", "monitor is paused")
    interval = int(match.get("interval") or 0)
    if interval > _MAX_PINGER_INTERVAL_SECONDS:
        return CheckResult(
            name, "FAIL", f"interval {interval}s > {_MAX_PINGER_INTERVAL_SECONDS}s; will sleep"
        )
    return CheckResult(name, "PASS", f"interval {interval}s; status={match.get('status')}")


def render_report(results: list[CheckResult]) -> str:
    lines: list[str] = []
    for result in results:
        first, *rest = (result.detail or "").split("\n")
        lines.append(
            f"{result.name:<{_NAME_WIDTH}}{result.status:<{_STATUS_WIDTH}}{first}".rstrip()
        )
        lines.extend(" " * (_NAME_WIDTH + _STATUS_WIDTH) + line for line in rest)
    failed = sum(1 for r in results if r.status == "FAIL")
    warned = sum(1 for r in results if r.status == "WARN")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    lines.append("")
    parts = []
    if failed:
        parts.append(f"{failed} failed")
    if warned:
        parts.append(f"{warned} warning" + ("s" if warned != 1 else ""))
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        lines.append(", ".join(parts) + f" -- see {_GUIDE_URL}")
    elif parts:
        lines.append("all checks passed, " + ", ".join(parts))
    else:
        lines.append("all checks passed")
    return "\n".join(lines)


def report_as_json(results: list[CheckResult]) -> dict:
    """The same content as render_report(), structured for a machine reader
    instead of an aligned-columns table -- built for --json, so a caller
    (e.g. Claude driving a guided deployment) can branch on an individual
    check's status/detail without re-parsing indentation-based continuation
    lines. `table` carries the exact render_report() text, so a caller that
    also owes a human the verbatim table (see .claude/commands/deploy.md)
    never has to re-run the checklist -- and its live checks (Render/GitHub/DB
    calls, one of which writes a webhook) -- a second time just to get both
    forms.
    """
    return {
        "checks": [
            {"name": r.name, "status": r.status, "detail": r.detail} for r in results
        ],
        "summary": {
            "passed": sum(1 for r in results if r.status == "PASS"),
            "warned": sum(1 for r in results if r.status == "WARN"),
            "failed": sum(1 for r in results if r.status == "FAIL"),
            "skipped": sum(1 for r in results if r.status == "SKIPPED"),
        },
        "guide_url": _GUIDE_URL,
        "table": render_report(results),
    }


def _wanted_env() -> dict[str, str]:
    """Local values for every var --sync-env will push.

    Keys depend on the selected provider: the nine always-synced vars (see
    _ALWAYS_SYNCED), plus LLM_PROVIDER, plus every provider's credential
    (the selected one always, the others only when they have a local value
    -- an opt-in .env lists the others empty, and must never be asked to
    fill them), plus every provider's model var.
    """
    wanted = {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_INSTALLATION_ID": str(settings.github_app_installation_id or ""),
        "GITHUB_APP_PRIVATE_KEY": settings.github_app_private_key,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "DASHBOARD_USERNAME": settings.dashboard_username,
        "DASHBOARD_PASSWORD": settings.dashboard_password,
        "DASHBOARD_SESSION_SECRET": settings.dashboard_session_secret,
        "RENDER_API_KEY": settings.render_api_key,
        "LLM_PROVIDER": settings.llm_provider,
    }
    entry = _PROVIDERS.get(settings.llm_provider)
    if entry is not None:
        credential, _ = entry
        wanted[credential] = getattr(settings, credential.lower(), "")
    for other_credential, model_var in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
        # EVERY provider's model var, not just the selected one's: a DB
        # provider override can activate any provider with no redeploy, and a
        # provider whose model var was never pushed would read a missing or
        # stale value on the service. All model vars have non-empty defaults,
        # so this can never trip the empty-value guard in sync_env().
        wanted[model_var] = getattr(settings, model_var.lower(), "")
    for credential, _ in _PROVIDERS.values():
        wanted.update(_override.local_slot_values(credential))
    for env_name, attr in _GENERIC_OPERATIONAL_ENV_ATTRS.items():
        wanted[env_name] = str(getattr(settings, attr))
    return wanted


def _wait_for_in_flight(service_id: str) -> bool:
    """Block until no deploy is building, or the timeout runs out.

    Waits rather than adopts: a deploy that started before the env-var push may
    have resolved its environment already, so adopting it could report "deploy
    live" for a container still running the old config.

    Returns True once settled (or nothing was building), False on timeout. The
    timeout path is deliberately a refusal, not a quiet return: a caller that
    ignored the return value and triggered anyway would stack a second deploy
    on one still building -- the exact collision this function exists to
    prevent. Do not "simplify" this back to a bare return.
    """
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    announced = False
    polls = 0
    deploy_id = None
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{_render.RENDER_API}/services/{service_id}/deploys",
            params={"limit": 1},
            headers=_render.headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        deploys = resp.json()
        if not deploys:
            return True
        deploy_obj = _render.unwrap(deploys[0], "deploy")
        deploy_id = deploy_obj.get("id")
        if deploy_obj.get("status") not in _DEPLOY_IN_FLIGHT_STATUSES:
            return True
        if not announced:
            print(f"waiting for in-flight deploy {deploy_id} to settle")
            announced = True
        elif polls % _IN_FLIGHT_PROGRESS_EVERY == 0:
            print(f"  still waiting for in-flight deploy {deploy_id} to settle")
        polls += 1
        time.sleep(_DEPLOY_POLL_SECONDS)
    print(
        f"timed out waiting for in-flight deploy {deploy_id} to settle -- refusing to "
        f"stack a second deploy on top of it; re-run once it finishes",
        file=sys.stderr,
    )
    return False


def _trigger_and_wait(service_id: str) -> int:
    """Render env-var changes do not auto-deploy, so a sync that skipped this
    would report success while the service kept serving the old values."""
    resp = httpx.post(
        f"{_render.RENDER_API}/services/{service_id}/deploys",
        headers=_render.headers(),
        json={},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    deploy_id = _render.unwrap(resp.json(), "deploy").get("id")
    print(f"deploy {deploy_id} triggered; waiting for live")
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(_DEPLOY_POLL_SECONDS)
        poll = httpx.get(
            f"{_render.RENDER_API}/services/{service_id}/deploys/{deploy_id}",
            headers=_render.headers(),
            timeout=_HTTP_TIMEOUT,
        )
        poll.raise_for_status()
        status = _render.unwrap(poll.json(), "deploy").get("status", "?")
        if status != last_status:
            print(f"  {status}")          # visible progress on a long build
            last_status = status
        if status == "live":
            print("deploy live")
            return 0
        if status == "canceled":
            print(
                f"deploy {deploy_id} was superseded (canceled) -- env vars WERE "
                f"pushed and a newer deploy is running; re-run to confirm it goes live",
                file=sys.stderr,
            )
            return 1
        if status in _DEPLOY_FAILED_STATUSES:
            print(f"deploy {status}", file=sys.stderr)
            return 1
    print("timed out waiting for the deploy to go live", file=sys.stderr)
    return 1


def _verify_database_url_reachable() -> str:
    """A human-readable status line about whether this write reaches the
    Render-hosted production database. Purely informational -- RENDER_API_KEY
    is optional for --sync-config-db, unlike --sync-env, so this never blocks
    the write. Never returns, prints, or logs a fetched Render value, only
    presence/absence and in-memory equality results (mirrors
    check_boot_credentials_live's and the former set_cooldown.py/
    set_usage_cap.py scripts' identical guard)."""
    if not settings.render_api_key:
        return (
            "could not verify against Render (no RENDER_API_KEY); "
            "writing without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); writing without live verification"
            )
        env_vars = _render.env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return (
            f"could not verify against Render ({type(exc).__name__}); "
            "writing without live verification"
        )
    if env_vars.get("DATABASE_URL") != settings.database_url:
        return (
            "could not confirm this DATABASE_URL is the one the Render "
            "service reads -- writing anyway"
        )
    return "DATABASE_URL verified against the live Render service"


# Column order shared by the SELECT and the INSERT ... ON CONFLICT below, so
# the two can never drift apart -- a name added to one without the other
# would silently read or write the wrong value.
_DB_SYNCED_COLUMNS = (
    "cooldown_base_seconds",
    "cooldown_max_seconds",
    "cooldown_factor",
    "key_usage_token_cap",
    "key_usage_reset_time_utc",
    "review_draft_prs",
)


def sync_config_db() -> int:
    """Push .env.config's usage-cap/cooldown/review-draft values into
    runtime_config, unconditionally -- these 6 keys are never a Render env var (see
    render.yaml and _DB_SYNCED_OPERATIONAL_KEYS above), so this is their only
    sync path. .env.config is the source of truth; the DB is only a mirror of
    it that the dispatcher actually reads (review_queue/cooldown_config.py,
    review_queue/usage_cap_config.py) -- see ISSUES.md's 2026-08-17 "two sources
    of truth" entry for why a Render env var was the wrong mirror target.

    Uses a raw, short-timeout connection rather than store.init_pool() /
    store.set_cooldown_override() -- same reason as _resolved_provider():
    a one-shot CLI must not pay the pool's 30s connect timeout.

    No CLI arguments: unlike the operator scripts this replaces
    (set_usage_cap.py, set_cooldown.py), there is exactly one source of
    truth (.env.config) and exactly one thing this does with it -- a partial,
    CLI-argument-driven write no longer exists, so there is nothing left to
    merge with the current DB value.

    key_usage_token_cap (gt=0) and dispatcher_rereview_cooldown_
    factor (ge=1.0) already have pydantic Field constraints -- an invalid
    value there fails at Settings() construction, long before this runs. The
    one invariant pydantic CANNOT express (it spans two fields) is
    base <= cap, so that -- plus the redundant-but-harmless base/cap
    positivity check, kept for a 1:1 match with
    cooldown_config.effective_config()'s own discard predicate -- is the only
    thing checked here.
    """
    if not settings.database_url:
        print("--sync-config-db requires DATABASE_URL", file=sys.stderr)
        return 2
    # Same risk sync_env() already guards against (ISSUES.md Parked Issues):
    # a shell that ran `eval "$(uv run python -m scripts.test_db)"` earlier
    # has a throwaway localhost:5433 URL sitting in os.environ, which Settings
    # reads ahead of any .env file -- this would silently write config into
    # that local container while an operator believes production was
    # updated. Deliberately the first guard, before any connection attempt.
    if _looks_like_local_test_db(settings.database_url):
        host = urlsplit(settings.database_url).hostname or "?"
        print(
            f"refusing to sync: DATABASE_URL points at {host}, a local/test Postgres -- "
            "this would write config into a database on this machine, not production. "
            "This is almost certainly a shell where "
            '`eval "$(uv run python -m scripts.test_db)"` was run; `unset DATABASE_URL` '
            "(or use a fresh shell) and re-run.",
            file=sys.stderr,
        )
        return 2
    base = settings.dispatcher_rereview_cooldown_seconds
    cap = settings.dispatcher_rereview_cooldown_max_seconds
    factor = settings.dispatcher_rereview_cooldown_factor
    if factor < 1.0 or base > cap or base <= 0 or cap <= 0:
        print(
            f"refusing to sync: cooldown would resolve to base={base} cap={cap} "
            f"factor={factor}, which effective_config() discards entirely (needs "
            "factor >= 1.0, 0 < base <= cap) -- fix .env.config first",
            file=sys.stderr,
        )
        return 2
    tokens = settings.key_usage_token_cap
    reset = settings.key_usage_reset_time_utc.isoformat()
    wanted = (base, cap, factor, tokens, reset, settings.review_draft_prs)

    # A column store.py's schema declares but the live table never got (CREATE
    # TABLE IF NOT EXISTS is a no-op against an already-provisioned table --
    # see _missing_runtime_config_columns) used to surface here as a raw
    # UndefinedColumn from the INSERT below. Checked before that INSERT is
    # even attempted, so the fix an operator needs is what they see, not a
    # driver exception.
    try:
        missing = _missing_runtime_config_columns()
    except psycopg.Error as exc:
        print(f"database error ({type(exc).__name__})", file=sys.stderr)
        return 2
    problem = _runtime_config_schema_problem(missing)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    print(_verify_database_url_reachable())
    now = datetime.now(timezone.utc).isoformat()
    columns = ", ".join(_DB_SYNCED_COLUMNS)
    placeholders = ", ".join(["%s"] * len(_DB_SYNCED_COLUMNS))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in _DB_SYNCED_COLUMNS)
    try:
        with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
            row = conn.execute(
                f"SELECT {columns} FROM runtime_config WHERE id = 1"
            ).fetchone()
            conn.execute(
                f"INSERT INTO runtime_config (id, {columns}, updated_at) "
                f"VALUES (1, {placeholders}, %s) "
                f"ON CONFLICT (id) DO UPDATE SET {assignments}, updated_at = EXCLUDED.updated_at",
                (*wanted, now),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"database error ({type(exc).__name__})", file=sys.stderr)
        return 2

    current = row if row is not None else (None,) * len(_DB_SYNCED_COLUMNS)
    changed = [
        f"{name} {old} -> {new}"
        for name, old, new in zip(_DB_SYNCED_COLUMNS, current, wanted)
        if old != new
    ]
    if changed:
        print("config->DB sync: " + ", ".join(changed))
    else:
        print("config->DB sync: already in sync")
    return 0


def sync_env() -> int:
    """Push local config to the Render service, then deploy and wait.

    Only ever uses the single-key endpoint: the bulk
    PUT /v1/services/{id}/env-vars replaces the entire list and would silently
    delete every variable not in the payload, DATABASE_URL included.
    """
    if not settings.render_api_key:
        print("--sync-env requires RENDER_API_KEY", file=sys.stderr)
        return 2
    # DATABASE_URL is in _ALWAYS_SYNCED, and Settings reads the process
    # environment ahead of any .env file -- so a shell that ran
    # `eval "$(uv run python -m scripts.test_db)"` (README's fast-iteration
    # path) has a throwaway localhost:5433 URL sitting in os.environ, and
    # _wanted_env() would happily push THAT to the live service, repointing
    # production at a container on the operator's laptop. This project has
    # already had one incident of exactly this shape (a live Render service
    # left holding a dummy test value -- see tests/conftest.py's
    # _quarantine_operator_apis comment), so a local-shaped URL is a refusal,
    # not a warning. Deliberately the FIRST guard after the API-key check:
    # everything below it would otherwise open psycopg connections to that
    # local database first and report confusing downstream failures. Consumes
    # scripts/_prereqs.py's predicate -- the same one scripts/test_db.py uses
    # -- so the two can never disagree about what "local" means.
    if _looks_like_local_test_db(settings.database_url):
        host = urlsplit(settings.database_url).hostname or "?"
        print(
            f"refusing to sync: DATABASE_URL points at {host}, a local/test Postgres -- "
            "pushing it would repoint the live Render service at a database on this "
            "machine. This is almost certainly a shell where "
            '`eval "$(uv run python -m scripts.test_db)"` was run; `unset DATABASE_URL` '
            "(or use a fresh shell) and re-run.",
            file=sys.stderr,
        )
        return 2
    if settings.llm_provider not in _PROVIDERS:
        accepted = ", ".join(sorted(_PROVIDERS))
        print(
            f"refusing to sync LLM_PROVIDER={settings.llm_provider!r}: "
            f"not a supported provider (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if settings.database_url:
        try:
            _, override = _resolved_provider()
        # deliberate: the provider check reports DB trouble
        except Exception:  # noqa: BLE001
            override = None
        if override and override != settings.llm_provider:
            print(
                f"refusing to sync: a DB provider override ({override}) is active and "
                f"wins over the LLM_PROVIDER={settings.llm_provider} being pushed. "
                "Clear it first: uv run python -m scripts.set_override --clear",
                file=sys.stderr,
            )
            return 2
        # Symmetric with the provider-override refusal above. An active model
        # override wins at runtime, so pushing a different model var would
        # report success while the service kept running the overridden model --
        # "what you pushed is what runs" has to stay true, not nearly true.
        # Checked for EVERY provider, not just the currently-active one:
        # _wanted_env() pushes every provider's model var (a DB provider flip
        # can activate any of them with no redeploy), so a non-active
        # provider's own DB model override can just as easily diverge from
        # what is about to be pushed for it. Resolved in ONE connection (not
        # one per provider in the loop below) via _resolved_model_overrides().
        try:
            model_overrides = _resolved_model_overrides()
        # deliberate: the provider check reports DB trouble
        except Exception:  # noqa: BLE001
            model_overrides = {}
        for provider in sorted(_PROVIDERS):
            model_override = model_overrides.get(provider)
            model_var = _PROVIDERS[provider][1]
            local_model = getattr(settings, model_var.lower(), "")
            if model_override and model_override != local_model:
                print(
                    f"refusing to sync: a DB model override ({model_override}) is active for "
                    f"{provider} and wins over the {model_var}={local_model} "
                    "being pushed. Clear it first: uv run python -m scripts.set_override "
                    f"{provider} --clear-model --no-activate",
                    file=sys.stderr,
                )
                return 2
    # Deliberately NOT inside the `if settings.database_url:` block above: this
    # is a pure local pricing-table lookup, so it must run whether or not a
    # database is configured. A warning, not a refusal (design spec 2026-08-18
    # section 6b): an unpriced model runs fine, it simply produces no cost
    # estimate on the review comment (providers/pricing.py::
    # estimate_cost_usd returns None), so there is nothing here worth blocking
    # the push over.
    for provider, model_var, model, known in _unpriced_models():
        print(
            f"warning: {model_var}={model!r} has no pricing-table entry for {provider} "
            f"(known: {known}); reviews will run without a cost estimate",
            file=sys.stderr,
        )
    wanted = _wanted_env()
    empty = sorted(
        key for key, value in wanted.items()
        if not value and key not in _OPTIONAL_EMPTY_ENV_KEYS
    )
    if empty:
        # This guard alone runs before any HTTP request, so refusing here
        # can never leave a partial push behind. Only the keys this provider
        # actually needs are in `wanted`, so this can never name another
        # provider's credential.
        print(
            f"refusing to push empty values; fix .env first: {', '.join(empty)}",
            file=sys.stderr,
        )
        return 2
    # Usage-cap/cooldown settings have no Render env var to push (see
    # _DB_SYNCED_OPERATIONAL_KEYS) -- this is their sync path. Deliberately
    # last of the pre-push guards, not first: sync_config_db() itself makes an
    # (informational, non-refusing) Render call, and every guard above this
    # promises to refuse before any HTTP request at all.
    config_db_exit = sync_config_db()
    if config_db_exit != 0:
        return config_db_exit
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            print(f"no Render service named {settings.render_service_name}", file=sys.stderr)
            return 1
        current = _render.env_vars(service_id)
        # `current.get(key)` is None for a var absent on Render -- normalize
        # to "" before comparing so an already-unset _OPTIONAL_EMPTY_ENV_KEYS
        # entry reads as in-sync instead of "changed" on every single run
        # (Render's API has no way to *store* an empty string -- see the PUT
        # vs. DELETE branch below -- so absent IS how empty is represented).
        changed = [key for key, value in wanted.items() if (current.get(key) or "") != value]
    # deliberate: nothing has been pushed yet, so a crashed lookup really is
    # "could not run at all"
    except Exception as exc:  # noqa: BLE001
        print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
        return 2

    # Tracks which keys have actually been written to the service. Once this
    # is non-empty, a later failure is a PARTIAL push, not a failure to
    # start -- it must be reported as exit 1 (with the pushed names), never
    # exit 2, or an operator would read "never really started" and walk away
    # from a service that is now half-configured.
    pushed: list[str] = []
    for key in changed:
        try:
            if wanted[key]:
                resp = httpx.put(
                    f"{_render.RENDER_API}/services/{service_id}/env-vars/{key}",
                    headers=_render.headers(),
                    json={"value": wanted[key]},
                    timeout=_HTTP_TIMEOUT,
                )
            else:
                # Render's PUT rejects an empty string outright (400: "must
                # provide a value or generateValue must be set to true") --
                # only reachable for an _OPTIONAL_EMPTY_ENV_KEYS entry (the
                # empty-value guard above refuses every other key), so unset
                # the var entirely instead; Settings' own field default
                # ("") applies when it's absent. A 404 here means it was
                # already absent -- also success, not a real failure.
                resp = httpx.delete(
                    f"{_render.RENDER_API}/services/{service_id}/env-vars/{key}",
                    headers=_render.headers(),
                    timeout=_HTTP_TIMEOUT,
                )
                if resp.status_code == 404:
                    pushed.append(key)
                    print(f"pushed {key} (len 0)")
                    continue
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            if pushed:
                return _report_partial_push(pushed, exc)
            # deliberate: no push has succeeded yet, so this is still
            # "could not run at all"
            print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
            return 2
        pushed.append(key)
        print(f"pushed {key} (len {len(wanted[key])})")   # names and lengths only
    if not changed:
        print("env vars already in sync; no deploy triggered")
        return 0
    try:
        if not _wait_for_in_flight(service_id):
            return 1
        return _trigger_and_wait(service_id)
    # deliberate: every key in `pushed` is already live on the service, so a
    # failure past this point is a partial push, not a failure to start
    except Exception as exc:  # noqa: BLE001
        return _report_partial_push(pushed, exc)


def _report_partial_push(pushed: list[str], exc: BaseException) -> int:
    """Exit 1, naming (never valuing) the vars that made it to the service
    before ``exc`` -- the operator's next step is to fix the problem and
    re-run --sync-env, not to assume nothing happened."""
    print(
        f"partial push: {', '.join(pushed)} already pushed to the service "
        f"before a {type(exc).__name__}; fix the problem and re-run --sync-env "
        "to finish (this is NOT \"could not run\" -- some vars already changed)",
        file=sys.stderr,
    )
    return 1


def _safe(name: str, fn, *args) -> CheckResult:
    """No check may abort the run: a complete table is the deliverable."""
    try:
        return fn(*args)
    # deliberate: any failure becomes a row
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"unexpected {type(exc).__name__}")


class CheckSpec(NamedTuple):
    """One row of the checklist, and the single source for its documentation.

    `verifies` is written for an operator reading the generated table, not for
    a maintainer reading this file -- scripts/gen_docs.py renders it verbatim.
    `required` means "always runs -- needs no operator-local key". It is NOT
    "can fail the run": `pricing` always runs but only ever WARNs. The others
    degrade to SKIPPED without RENDER_API_KEY / UPTIMEROBOT_API_KEY /
    DATABASE_URL rather than failing.
    """

    name: str
    func: Callable[..., CheckResult]
    verifies: str
    required: bool
    args: tuple[str, ...] = ()


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("config", lambda: check_config(),
              "Every setting the service needs is resolvable locally", True),
    CheckSpec("pricing", lambda: check_pricing(),
              "Every provider's effective model has a rate-table entry "
              "(a warning only -- an unpriced model runs, without a cost estimate)",
              True),
    CheckSpec("boot-creds-live", lambda: check_boot_credentials_live(),
              "The vars the service reads at every boot are present on the deployed "
              "Render service under their current names -- not just locally", True),
    CheckSpec("github-app",
              lambda repos, base: check_installation_and_webhook(repos, base),
              "The App has exactly one installation, every repo in GITHUB_TARGET_REPO "
              "is covered by it, and its webhook points here (set only if wrong)",
              True, ("repos", "base")),
    CheckSpec("health", lambda base: check_health_endpoint(base),
              "/healthz answers BOTH GET and HEAD -- UptimeRobot's free tier sends "
              "HEAD, so a GET-only endpoint lets the instance sleep", True, ("base",)),
    CheckSpec("database", lambda: check_database(),
              "Postgres is reachable and the app has provisioned its tickets table",
              False),
    CheckSpec("runtime-config", lambda: check_runtime_config_schema(),
              "runtime_config has every column store.py's schema declares -- a column "
              "added after the table was first provisioned is never backfilled by the "
              "app's own CREATE TABLE IF NOT EXISTS boot DDL", False),
    CheckSpec("provider", lambda: check_provider(),
              "The provider that will actually run -- LLM_PROVIDER, or an active DB "
              "override -- has its credential set", False),
    CheckSpec("provider-live", lambda: check_provider_live(),
              "The actively-resolved provider's credential is present on the deployed "
              "Render service, not just locally", True),
    CheckSpec("api-key-live", lambda: check_api_key_live(),
              "The actively-resolved provider's actively-resolved key slot is present "
              "on the deployed Render service", True),
    CheckSpec("render-service", lambda: check_render_service(),
              "The latest Render deploy is live, and matches local HEAD when a commit "
              "is comparable", True),
    CheckSpec("uptime-pinger", lambda base: check_uptime_pinger(base),
              "A monitor targets /healthz exactly, is active, and polls at most every "
              "10 minutes", True, ("base",)),
)


def run_checks(repos: frozenset[str], base: str) -> list[CheckResult]:
    """All twelve, foundational (and cheap, where possible) first, so a
    misconfiguration is reported before the checks that would fail as a
    consequence of it. Order and content come from CHECKS, which
    scripts/gen_docs.py also renders -- so the table an operator reads can
    never describe a different set than the one that runs.
    """
    available = {"repos": repos, "base": base}
    return [
        _safe(spec.name, spec.func, *(available[name] for name in spec.args))
        for spec in CHECKS
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy",
        # Without this, argparse treats --sync-en as an abbreviation of
        # --sync-env and RUNS the sync. Not hypothetical: it fired a real
        # deploy against live infrastructure during development.
        allow_abbrev=False,
        description=(
            "Verify the hosted deployment: configuration, whether the credentials the "
            "service needs at every boot are actually live on Render, GitHub App "
            "installation and webhook, health endpoint, database, active provider, "
            "whether that provider's credential is actually live on Render, whether its "
            "active API-key slot is actually live on Render, Render service, and "
            "keep-warm pinger. Exit 0 all passed, 1 a check failed, 2 could not run."
        ),
    )
    parser.add_argument(
        "--sync-env",
        action="store_true",
        help=(
            "push local config to the Render service, deploy, and wait for live "
            "(worst case ~30 minutes: up to 900s waiting out an in-flight deploy, "
            "then up to 900s for the newly triggered one)"
        ),
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help=(
            "check only /healthz and exit -- needs just PUBLIC_BASE_URL/"
            "RENDER_EXTERNAL_URL, no credential"
        ),
    )
    parser.add_argument(
        "--sync-config-db",
        action="store_true",
        help=(
            "push .env.config's usage-cap/cooldown values into the runtime_config "
            "database only -- no Render calls, no checklist, no redeploy; takes "
            "effect on the next claimed ticket. Needs only DATABASE_URL"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "emit the checklist (from a plain run or --health-only) as one JSON "
            "object -- {checks: [{name, status, detail}], summary, guide_url, "
            "table} -- instead of the plain-text table; `table` carries the same "
            "text the non-JSON mode prints, for a caller that owes a human the "
            "verbatim table too. Has no effect on --sync-config-db's output, and "
            "not on --sync-env's own progress lines -- only on the checklist a "
            "plain run (with or without --sync-env first) or --health-only prints"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if sum([args.sync_env, args.health_only, args.sync_config_db]) > 1:
        print(
            "--sync-env, --health-only, and --sync-config-db are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.sync_config_db:
        return sync_config_db()
    base = resolve_base_url()
    if args.health_only:
        if not base:
            print(
                "a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) is required",
                file=sys.stderr,
            )
            return 2
        result = check_health_endpoint(base)
        print(json.dumps(report_as_json([result])) if args.json else render_report([result]))
        return 1 if result.status == "FAIL" else 0
    if not base:
        print(
            "a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) is required",
            file=sys.stderr,
        )
        return 2
    if args.sync_env:
        exit_code = sync_env()
        if exit_code != 0:
            return exit_code
    results = run_checks(settings.target_repos(), base)
    print(json.dumps(report_as_json(results)) if args.json else render_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
