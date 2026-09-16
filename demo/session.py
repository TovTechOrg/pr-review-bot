"""Per-visitor session tracking, in memory, with TTL eviction.

A public link must not grow the process's memory without bound, and one
reader must never see another's review.

Two ContextVars carry the request-scoped identity of "who is this, and which
provider did they ask for" down into demo/store.py's ``enqueue_or_update``.
That works only because the demo's self-delivery (demo/trigger.py) POSTs to
its own ``/webhook`` over an in-process ASGI transport, INSIDE the bootstrap
request's own async context -- a child task inherits the context it was
created in. It emphatically does NOT reach the dispatcher, which is a
long-lived background task created once in main.py's lifespan, in a context
that predates every request: that hop is made by the ticket-context side
table and the "currently processing" slot in demo/store.py instead.
"""

from __future__ import annotations

import contextvars
import time

from dashboard.auth import SESSION_COOKIE_NAME

TTL_SECONDS = 60 * 60

# The single, stable identity every cookie-hostile visitor shares (LinkedIn's
# in-app browser, private modes). Deliberately a constant rather than
# something minted per request: the spec's fallback is a *stateless shared
# view*, and a per-request identity would also break webhook.py's delivery
# dedup (demo/trigger.py derives the delivery id from this) and grow
# `_last_seen` without bound.
SHARED_SESSION_ID = "stateless-shared"

_last_seen: dict[str, float] = {}

current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "demo_current_session", default=None
)
current_provider: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "demo_current_provider", default=None
)


def reset() -> None:
    _last_seen.clear()


def touch(session_id: str, now: float | None = None) -> None:
    _last_seen[session_id] = time.monotonic() if now is None else now


def is_known(session_id: str) -> bool:
    return session_id in _last_seen


def sweep(now: float | None = None) -> list[str]:
    """Evict every session idle for longer than TTL_SECONDS.

    Returns the evicted ids (not just a count) because the caller --
    demo/app.py's periodic sweep task -- needs them to drop those sessions'
    tickets and reviews from demo/store.py too. A count alone left the
    store growing forever, which is what made this function dead code in
    practice even once it ran.
    """
    current = time.monotonic() if now is None else now
    stale = [sid for sid, seen in _last_seen.items() if current - seen > TTL_SECONDS]
    for sid in stale:
        _last_seen.pop(sid, None)
    return stale


def session_id_for(request) -> str | None:
    """The dashboard session cookie, or None when cookies are unavailable.

    None is the cookie-hostile case (LinkedIn's in-app browser, private
    modes). Callers degrade to the shared stateless view rather than erroring.
    """
    return request.cookies.get(SESSION_COOKIE_NAME)
