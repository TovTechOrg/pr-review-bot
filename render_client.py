"""Shared Render API access. Used by:
- scripts/deploy.py, scripts/set_override.py (via scripts/_override.py),
  scripts/reset_queue.py -- as CLI/operator support code.
- dashboard/environment.py -- as production runtime code (the dashboard's
  Environment tab). This is why this module lives at the repo root, not
  scripts/: scripts/ is operator-CLI-only, and dashboard/environment.py
  must not import from it. See docs/superpowers/specs/
  2026-09-02-dashboard-environment-tab-design.md.

Consolidates what was previously duplicated Render-fetch logic across
scripts; see docs/superpowers/specs/2026-08-10-render-access-consolidation-design.md.
"""

from __future__ import annotations

import logging
import os

import httpx

from config import settings

logger = logging.getLogger(__name__)

RENDER_API = "https://api.render.com/v1"
HTTP_TIMEOUT = 10.0
_ENV_VARS_PAGE_LIMIT = 100


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def find_service_id() -> str | None:
    """Locate this deployment's own Render service.

    RENDER_SERVICE_ID is one of Render's own automatically-injected,
    platform-reserved env vars (present on every Render-hosted process,
    alongside RENDER_SERVICE_NAME/RENDER_EXTERNAL_URL/RENDER_GIT_COMMIT/etc)
    -- it's the service's id directly, needing no API call or name match at
    all when running on Render (this var is always present there). This is
    what setting a custom RENDER_SERVICE_NAME env var to try to identify the
    service was working around, less reliably: that name collides with
    Render's own reserved var of the identical name (which holds the
    service's *slug*, e.g. "pr-review-bot-km8b", not its display Name), and
    the platform's own injected value silently overrides a same-named
    custom one in the actual running container regardless of what the
    dashboard shows as "configured" -- confirmed live, 2026-09-03: setting
    RENDER_SERVICE_NAME to the plain display name never took effect across
    several redeploys, because it can't.

    Only when NOT running on Render (a developer's own machine, running
    scripts/* -- where RENDER_SERVICE_ID is never auto-injected) does
    this fall back to the name-based lookup: matching
    `settings.render_service_name` -- an operator-typed value in that
    context -- against the service list's `name` field, same as before.
    """
    reserved_id = os.environ.get("RENDER_SERVICE_ID")
    if reserved_id:
        return reserved_id

    resp = httpx.get(f"{RENDER_API}/services", headers=headers(), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    names = []
    for item in body:
        service = unwrap(item, "service")
        names.append(service.get("name"))
        if service.get("name") == settings.render_service_name:
            return service.get("id")
    # Service names are not secret -- logging them (and the configured
    # target) is what makes a silent "not found" diagnosable at all.
    logger.warning(
        "render_client.find_service_id: no service named %r among %d returned (%r)",
        settings.render_service_name, len(body), names,
    )
    return None


def env_vars(service_id: str) -> dict[str, str]:
    """The service's live env-vars, key -> value.

    Callers must reduce a returned value to a boolean or an equality result
    immediately -- never store it beyond that computation, print it, or pass
    it to anything that might log it -- UNLESS the caller is
    dashboard/environment.py's GET /api/environment/render, the one
    documented, scoped exception to that rule (root CLAUDE.md's Secret
    handling section). See CLAUDE.md's "no secret is ever logged" and
    docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md
    section 6.

    Render paginates this endpoint (cursor-based, each item carries its own
    "cursor" field) -- a service with more vars than one page silently
    dropped everything past the first page here until this loop was added,
    which made every caller blind to any var that happened to land on page
    2+. Confirmed live: this project's Render service carries 29 vars
    against a 20-per-page default, with DATABASE_URL and
    VERTEX_GCP_SERVICE_ACCOUNT_KEY both on page 2.
    """
    current: dict[str, str] = {}
    params: dict[str, int | str] = {"limit": _ENV_VARS_PAGE_LIMIT}
    while True:
        resp = httpx.get(
            f"{RENDER_API}/services/{service_id}/env-vars",
            headers=headers(),
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json()
        for item in page:
            env_var = unwrap(item, "envVar")
            current[env_var.get("key")] = env_var.get("value")
        if len(page) < _ENV_VARS_PAGE_LIMIT:
            return current
        params = {"limit": _ENV_VARS_PAGE_LIMIT, "cursor": page[-1]["cursor"]}


# Every var main.py's lifespan() either explicitly refuses to boot
# without, or implicitly hard-depends on (DATABASE_URL for store.init_pool(),
# GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY for the installation-id verification
# call), plus RENDER_API_KEY itself -- deleting it would strand this feature's
# own ability to fix anything else. Never deletable via push_env_var/
# delete_env_var's caller, dashboard/environment.py -- see
# docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md.
PROTECTED_ENV_KEYS = frozenset(
    {
        "DATABASE_URL",
        "RENDER_API_KEY",
        "DASHBOARD_USERNAME",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_SESSION_SECRET",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
    }
)


class ProtectedEnvKeyError(Exception):
    """Raised by delete_env_var() for a PROTECTED_ENV_KEYS member -- the
    caller (dashboard/environment.py) reports this as a per-key "protected"
    failure rather than attempting the delete."""


def push_env_var(service_id: str, key: str, value: str) -> None:
    """Single-key PUT -- never the bulk endpoint (PUT /env-vars, plural,
    silently replaces the whole list). Raises on failure; the caller decides
    how to report it -- see dashboard/environment.py's per-key loop."""
    resp = httpx.put(
        f"{RENDER_API}/services/{service_id}/env-vars/{key}",
        headers=headers(),
        json={"value": value},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def delete_env_var(service_id: str, key: str) -> None:
    """Single-key DELETE. Refuses outright for a PROTECTED_ENV_KEYS member --
    never even issues the HTTP request."""
    if key in PROTECTED_ENV_KEYS:
        raise ProtectedEnvKeyError(key)
    resp = httpx.delete(
        f"{RENDER_API}/services/{service_id}/env-vars/{key}",
        headers=headers(),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def trigger_deploy(service_id: str) -> str:
    """Fire-and-forget: POST the deploy trigger and return its id
    immediately -- no polling. Unlike scripts/deploy.py's own
    _trigger_and_wait (which blocks until "live" for the CLI's benefit), a
    caller running inside the very service being redeployed cannot safely
    block on that -- the container may be torn down mid-poll once the new
    one passes its health check."""
    resp = httpx.post(
        f"{RENDER_API}/services/{service_id}/deploys",
        headers=headers(),
        json={},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return unwrap(resp.json(), "deploy")["id"]
