"""Per-visitor session tracking, in memory, with TTL eviction.

A public link must not grow the process's memory without bound, and one
reader must never see another's review.
"""

from __future__ import annotations

import contextvars
import time

from dashboard.auth import SESSION_COOKIE_NAME

TTL_SECONDS = 60 * 60

_last_seen: dict[str, float] = {}

current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "demo_current_session", default=None
)


def reset() -> None:
    _last_seen.clear()


def touch(session_id: str, now: float | None = None) -> None:
    _last_seen[session_id] = time.monotonic() if now is None else now


def is_known(session_id: str) -> bool:
    return session_id in _last_seen


def sweep(now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    stale = [sid for sid, seen in _last_seen.items() if current - seen > TTL_SECONDS]
    for sid in stale:
        _last_seen.pop(sid, None)
    return len(stale)


def session_id_for(request) -> str | None:
    """The dashboard session cookie, or None when cookies are unavailable.

    None is the cookie-hostile case (LinkedIn's in-app browser, private
    modes). Callers degrade to the shared stateless view rather than erroring.
    """
    return request.cookies.get(SESSION_COOKIE_NAME)
