from datetime import time

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Env-var names that hold plain operational config, not credentials. LISTED =
# OPERATIONAL (lives in .env.config, freely editable by anyone including an
# agent); EVERYTHING ELSE IS SECRET BY DEFAULT (lives in .env, which an agent
# must never open -- see CLAUDE.md's "Secret handling" section).
#
# Every entry is a LITERAL key name, enumerated one by one -- never a prefix or
# glob. A pattern would silently classify future keys that happen to match,
# which is exactly the secret-by-default guarantee this list exists to provide.
#
# Adding a setting here is a deliberate classification decision, not a
# formality: tests/test_config.py fails if a listed key is found in .env or an
# unlisted key is found in .env.config.
OPERATIONAL_KEYS = frozenset(
    {
        "LLM_PROVIDER",
        "GEMINI_MODEL",
        "GROQ_MODEL",
        "VERTEX_MODEL",
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_RESET_TIME_UTC",
        "VERTEX_GCP_PROJECT",
        "VERTEX_GCP_LOCATION",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "DISPATCHER_IDLE_SLEEP_SECONDS",
        "DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS",
        "DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS",
        "DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS",
        "DISPATCHER_MAX_FAILURE_ATTEMPTS",
        "DISPATCHER_MAX_NOTICE_POST_ATTEMPTS",
        "DISPATCHER_MIN_RETRY_AFTER_SECONDS",
        "DISPATCHER_BACKOFF_JITTER_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_FACTOR",
        "DISPATCHER_NOTICE_SWEEP_BATCH_SIZE",
        "RENDER_SERVICE_NAME",
        "GITHUB_TARGET_REPO",
        "PUBLIC_BASE_URL",
        "REVIEW_DRAFT_PRS",
    }
)


class Settings(BaseSettings):
    # Two files, one Settings. .env holds credentials and identity; .env.config
    # holds operational settings (OPERATIONAL_KEYS above). The LAST file wins on
    # a key present in both, so .env.config -- the designated home -- outranks a
    # stale line left in .env. A real process env var still beats both, which is
    # why Render is unaffected: neither file exists in the container.
    model_config = SettingsConfigDict(env_file=(".env", ".env.config"), extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _blank_values_fall_back_to_defaults(cls, data):
        """A key present in .env/.env.config with nothing after the `=` (e.g.
        an unfilled template line like `GITHUB_APP_INSTALLATION_ID=`) reaches
        here as the literal string "" -- pydantic then tries to coerce that
        into the field's real type (int, time, ...) and raises at import
        time, rather than falling back to the default the way a fully absent
        var already does. Drop any blank value so "not filled in yet" behaves
        identically whether the var is absent or present-but-empty (design
        spec 2026-08-18 section 6e's "never crash at import" intent already
        covered the absent case; this closes the present-but-blank gap it
        missed).
        """
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value != ""}
        return data

    github_app_id: int = 0
    github_app_installation_id: int = 0
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    # Required (see main.py's lifespan and scripts/deploy.py's check_config):
    # "*" to act on every repo the App's installation covers, or a
    # comma-separated allowlist of specific "owner/repo" entries. See
    # target_repos()/default_target_repo() below for how this is parsed.
    github_target_repo: str = ""
    public_base_url: str = ""  # set from RENDER_EXTERNAL_URL on Render; PUBLIC_BASE_URL override

    # --- Dashboard authentication. A single shared operator credential (no
    # per-user accounts) gates dashboard/router.py's router -- see
    # docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md.
    # dashboard_session_secret signs the session-cookie JWT and is
    # independent of the password: rotating it invalidates every active
    # session at once, the deliberate "revoke everything" lever if a session
    # is ever suspected compromised.
    dashboard_username: str = ""
    dashboard_password: str = ""
    dashboard_session_secret: str = ""

    # No implicit default: guessing a provider means silently running (and
    # billing) against one the operator never chose. Validated in
    # main.py's lifespan rather than as a pydantic required field -- a
    # required field would raise the moment anything first reads `settings`
    # (see the lazy `__getattr__` below), breaking pytest and
    # scripts/doctor.py before either could report the problem (design spec
    # 2026-08-18 section 6e).
    llm_provider: str = ""
    # ``gemini_model`` is consumed by the gemini provider only. Groq is a
    # different model family (Llama, via a different vendor), so it
    # gets its own var — a single shared GEMINI_MODEL became ambiguous the moment
    # a second provider family entered the picture (see CLAUDE.md task 8 / PR
    # report for the reasoning).
    gemini_model: str = "gemini-flash-latest"

    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Vertex's own model var. Default is the model confirmed live against this
    # project's Vertex catalog; the gemini default (gemini-flash-latest) 404s
    # there, which is exactly why these two no longer share a var.
    vertex_model: str = "gemini-2.5-flash"

    # --- Vertex AI (LLM_PROVIDER=vertex). Unlike gemini/groq, the credential
    # is a GCP service-account identity rather than an API-key string:
    # VERTEX_GCP_SERVICE_ACCOUNT_KEY (hosted, always base64) -> implicit ADC. See
    # providers/vertex_credentials.py for the resolution order.
    # An OPTIONAL override: unset means "use the project_id embedded in the
    # resolved service-account key", so an operator handed nothing but a JSON
    # key needs no separate project lookup.
    vertex_gcp_project: str = ""
    # Which Vertex regional endpoint to call -- not an account property, so the
    # default needs no lookup either.
    vertex_gcp_location: str = "us-central1"
    vertex_gcp_service_account_key: str = ""
    # Ceiling on a single LLM request, in seconds. The dispatcher is a single
    # serial consumer of the whole queue (review_queue/dispatcher.py) -- a hung
    # call with no timeout would stall every pending PR's review, not just
    # one, for however long the SDK's own default timeout is (several
    # minutes). 45s is well under that while still tolerating a genuinely
    # slow-but-healthy response.
    llm_request_timeout_seconds: float = 45.0

    database_url: str = ""
    dispatcher_idle_sleep_seconds: float = 1.0
    dispatcher_default_retry_after_seconds: float = 60.0
    dispatcher_failure_base_backoff_seconds: float = 2.0
    dispatcher_failure_max_backoff_seconds: float = 300.0
    dispatcher_max_failure_attempts: int = 5
    dispatcher_max_notice_post_attempts: int = 3
    dispatcher_min_retry_after_seconds: float = 1.0
    dispatcher_backoff_jitter_seconds: float = 0.0
    dispatcher_rereview_cooldown_seconds: float = 300.0
    dispatcher_rereview_cooldown_max_seconds: float = 3600.0
    # ge=1.0: a factor < 1 would shrink the cooldown across escalation
    # levels instead of lengthening it, defeating the point of escalation.
    dispatcher_rereview_cooldown_factor: float = Field(default=2.0, ge=1.0)
    # gt=0: 0 would silently disable the notice sweep entirely, and -1 means
    # "no limit" in SQLite, silently reverting to the unbounded pre-fix
    # behavior this setting exists to prevent.
    dispatcher_notice_sweep_batch_size: int = Field(default=20, gt=0)

    # --- Draft PRs. Database-only, like the cooldown/usage-cap settings
    # above -- never a Render env var, so an operator can flip it with no
    # redeploy (uv run python -m scripts.deploy --sync-config-db). False
    # (the default) skips a review while a PR is a draft; ready_for_review
    # still triggers one even with zero new commits (see webhook.py).
    review_draft_prs: bool = False

    # --- Proactive per-key daily usage cap. Defaults to None (feature off): a
    # deployment that sets no env var behaves exactly as before. The reset
    # time is a plain "HH:MM" (or "HH:MM:SS") UTC wall-clock string; a `time`
    # field makes pydantic parse it, giving arbitrary granularity rather than
    # whole-hour-only resets, specifically so a demo can set the reset a
    # couple of minutes out instead of waiting for the next hour.
    # gt=0: a 0 or negative cap would make the dispatcher's `tokens >= 0`
    # comparison unconditionally true, deferring every review forever -- and
    # that deferral is STICKY, since a ticket's not_before is already set to
    # a real future timestamp by the time it happens, so fixing the env var
    # and redeploying does not release already-deferred tickets.
    key_usage_token_cap: int | None = Field(default=None, gt=0)
    key_usage_reset_time_utc: time = Field(default=time(4, 0))

    # --- Required operator tooling: read by scripts/deploy.py on the
    # operator's own machine, and (RENDER_API_KEY only) by the deployed
    # service itself for the dashboard's Environment tab. Never added to
    # render.yaml directly -- RENDER_API_KEY reaches the service via
    # --sync-env instead. Absence now FAILs the checks that need it, never
    # SKIPs.
    uptimerobot_api_key: str = ""
    render_api_key: str = ""
    render_service_name: str = "pr-review-engine"

    def target_repos(self) -> frozenset[str]:
        """Configured repo allowlist, or empty (= no restriction -- act on
        every repo this App's installation is registered with).

        `GITHUB_TARGET_REPO=*` is the explicit, operator-set spelling of "no
        restriction" -- a real, non-empty value that can be pushed to Render
        and tuned there like every other operational key, unlike the old
        bare-empty-string sentinel (which needed a special exemption from
        --sync-env's "refuse to push empty values" guard; see
        scripts/deploy.py's _OPTIONAL_EMPTY_ENV_KEYS history). A left-over
        empty string still parses to the same "no restriction" frozenset
        here (so nothing crashes), but it's no longer a valid *configured*
        state -- main.py's lifespan and scripts/deploy.py's check_config()
        both refuse to start/deploy on an empty value, forcing an operator to
        pick "*" or a real allowlist rather than leaving it unset by
        accident.

        ',' is a safe delimiter for the allowlist form: GitHub repo names may
        only contain ASCII letters, digits, '.', '-', and '_', and
        account/org names only alphanumeric characters and '-' -- a comma
        can never occur inside a genuine "owner/repo" value, so splitting on
        it can't misinterpret a real repo's name.
        """
        value = self.github_target_repo.strip()
        if value == "*":
            return frozenset()
        return frozenset(r.strip() for r in value.split(",") if r.strip())

    def is_target_repo_configured(self) -> bool:
        """Whether GITHUB_TARGET_REPO has been explicitly set at all ("*" or
        a real allowlist) -- distinct from target_repos() being empty, which
        is also true for the unconfigured state (see target_repos()'s
        docstring). Setup-time tooling (scripts/doctor.py) needs this
        distinction to tell "not configured yet" apart from a deliberate
        "*"; main.py's lifespan and scripts/deploy.py's check_config() use
        the plain falsy check directly since they only care about the
        unconfigured case."""
        return bool(self.github_target_repo.strip())

    def default_target_repo(self) -> str:
        """The first entry of GITHUB_TARGET_REPO's comma-separated list (or
        "" if unset or "*") -- for a manual/demo script that operates against
        exactly one repo (seed_demo_pr.py, demo_provider_swap.py), never
        target_repos() itself: that returns an unordered frozenset, correct
        for webhook.py's membership check but wrong here, where a single
        deterministic repo is needed. Reading github_target_repo directly
        would break under multi-repo config -- the raw field is the whole
        comma-joined string, not a single repo. "*" has no single repo to
        return either -- those scripts need a real testbed repo configured,
        not "act on everything"."""
        value = self.github_target_repo.strip()
        if value == "*":
            return ""
        first, _, _ = value.partition(",")
        return first.strip()


# Deliberately lazy: constructing Settings() reads and validates the real
# .env/.env.config, and raises ValidationError if either genuinely
# misconfigures a field (not just leaves one blank -- see
# _blank_values_fall_back_to_defaults above for that case). Building it
# eagerly at import time meant merely IMPORTING this module -- even just for
# the Settings class itself, never touching this singleton -- forced that
# validation immediately. scripts/init_env.py hit exactly this: it imports
# Settings only to validate one answer at a time via Settings.model_validate,
# but a single already-malformed value left over in .env/.env.config (e.g.
# from before this file's own blank-value fix existed) crashed the import
# with a raw traceback before init_env's own code ever ran.
#
# This __getattr__ defers construction to first access instead, so importing
# this module no longer forces validation of whatever is currently on disk.
# It does NOT change whether a genuinely invalid value raises -- only when:
# the app's own boot path (main.py) still reads `settings` immediately on
# startup, so a real misconfiguration still fails loudly there, exactly as
# before and as the existing tests for this (e.g.
# test_key_usage_reset_time_rejects_garbage) already pin.
def __getattr__(name: str) -> Settings:
    if name == "settings":
        global settings
        settings = Settings()
        return settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
