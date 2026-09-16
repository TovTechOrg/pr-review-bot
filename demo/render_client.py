"""Fake Render env-var listing for the demo's Environment panel.

No real Render API key exists on the demo service, so there is nothing real
to display. Values are a fixed mask: the real dashboard masks by default, and
here there is no underlying value at all.
"""

from __future__ import annotations

MASK = "*" * 8

_KEYS = (
    "DATABASE_URL", "GITHUB_APP_ID", "GITHUB_WEBHOOK_SECRET",
    "GITHUB_TARGET_REPO", "DASHBOARD_USERNAME", "GROQ_API_KEY",
)


def env_vars() -> list[dict]:
    return [{"key": key, "value": MASK} for key in _KEYS]


def update_env_var(key: str, value: str) -> dict:
    """Report success, change nothing. A reload returns the canned defaults."""
    return {"applied": True, "key": key}
