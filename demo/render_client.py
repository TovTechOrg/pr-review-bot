"""In-memory stand-in for render_client.py. Reaches no network.

The demo service holds no Render API key, so the real module's very first
call (`find_service_id()` -> `httpx.get("https://api.render.com/...")`) would
go out over the wire with an empty bearer token, 401, and 500 the dashboard's
whole Environment panel -- the opposite of the design's "nothing can fail
because a third party is down".

Every name here matches the real module's signature exactly, because
dashboard/environment.py calls them on the module object and
demo/app.py::install_mocks() rebinds them there: `env_vars` takes a
service_id and returns key -> value (the real shape), not a list of rows.
Values are fixed, synthetic, and carry no authority anywhere -- the
dashboard masks them by default exactly as it masks real ones. Writes report
success and persist nothing, so a reload returns these same defaults.
"""

from __future__ import annotations

import logging

# Re-exported verbatim rather than restated: the protected-key set is policy,
# not integration, and a demo copy that drifted from it would quietly show a
# delete control the real dashboard refuses.
from render_client import PROTECTED_ENV_KEYS, ProtectedEnvKeyError  # noqa: F401

logger = logging.getLogger(__name__)

DEMO_SERVICE_ID = "srv-demo000000000000000"

# Plausible-looking, entirely synthetic. Nothing here authenticates anything,
# and no real value of any kind exists on the demo service to leak.
_ENV_VARS: dict[str, str] = {
    "DATABASE_URL": "postgresql://demo:demo@demo-db.internal:5432/demo",
    "GITHUB_APP_ID": "123456",
    "GITHUB_APP_INSTALLATION_ID": "7654321",
    "GITHUB_WEBHOOK_SECRET": "demo-webhook-secret-not-real",
    "GITHUB_TARGET_REPO": "bot-demo/example-app",
    "DASHBOARD_USERNAME": "demo",
    "DASHBOARD_PASSWORD": "demo",
    "DASHBOARD_SESSION_SECRET": "demo-session-secret-not-real-0000",
    "RENDER_API_KEY": "rnd_demoDEMOdemoDEMOdemo",
    "GEMINI_API_KEY": "demo-gemini-key-not-real",
    "GROQ_API_KEY": "demo-groq-key-not-real",
    # Deliberately NOT base64-shaped, unlike the real var: a plausible-looking
    # blob here trips the repo's gitleaks pre-commit hook on every commit that
    # touches this file, and the panel only ever displays this string.
    "VERTEX_GCP_SERVICE_ACCOUNT_KEY": "demo-service-account-not-real",
}


def headers() -> dict[str, str]:
    """Never used (nothing here makes a request); present so this module's
    shape matches the real one it replaces."""
    return {"Accept": "application/json"}


def unwrap(item: dict, key: str) -> dict:
    return item.get(key) or item


def find_service_id() -> str | None:
    return DEMO_SERVICE_ID


def env_vars(service_id: str) -> dict[str, str]:
    return dict(_ENV_VARS)


def push_env_var(service_id: str, key: str, value: str) -> None:
    """Reports success and changes nothing -- a reload returns the canned
    defaults. Logged by name and length only, never by value, matching the
    real deployment path's convention."""
    logger.info("demo render_client: pretended to push %s (len %d)", key, len(value))


def delete_env_var(service_id: str, key: str) -> None:
    """Refuses a protected key exactly as the real one does, so the
    dashboard's protected-key handling is demonstrated rather than bypassed."""
    if key in PROTECTED_ENV_KEYS:
        raise ProtectedEnvKeyError(key)
    logger.info("demo render_client: pretended to delete %s", key)


def trigger_deploy(service_id: str) -> str:
    return "dep-demo000000000000000"
