"""Fires a real, correctly-signed webhook delivery at our own /webhook.

The claim the demo makes is "this is the engine", so the delivery runs the
real signature check, the real dedup, the real 202, the real ticket enqueue
and the real dispatcher. A bypass route would quietly make that claim false.

Repeat loads inside one session are absorbed by webhook.py's existing dedup:
the delivery id is derived from the session, so a refresh is a 200 no-op
returning the same review rather than a second ticket.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from config import settings
from demo.content import DEMO_HEAD_SHA, DEMO_PR_NUMBER, DEMO_PR_TITLE, DEMO_REPO
from demo.session import SHARED_SESSION_ID, current_provider, current_session

_SHARED_SESSION = SHARED_SESSION_ID

# The demo PR's own number is kept for the shared/cookie-less view (the one
# the docs and the article's screenshots describe); every cookie-carrying
# session gets its own number in a plausible range above it.
_PR_NUMBER_SPREAD = 8_000


def delivery_id_for(session_id: str | None) -> str:
    basis = session_id or _SHARED_SESSION
    return hashlib.sha256(f"demo-delivery-{basis}".encode()).hexdigest()[:32]


def pr_number_for(session_id: str | None) -> int:
    """A stable, per-session pull-request number.

    review_queue/store.py dedups on (repo_full_name, pr_number) -- the real
    ON CONFLICT key, which demo/store.py mirrors faithfully. With a single
    fixed PR number every concurrent visitor collapsed onto one shared
    ticket, so one reader's review was the only one that could exist. Giving
    each session its own PR number scopes the ticket exactly where a real
    deployment scopes it (the identity of the pull request) rather than
    bending the mock store's dedup semantics away from the real one's.
    """
    if session_id is None or session_id == _SHARED_SESSION:
        return DEMO_PR_NUMBER
    digest = hashlib.sha256(f"demo-pr-{session_id}".encode()).hexdigest()[:8]
    return DEMO_PR_NUMBER + 1 + int(digest, 16) % _PR_NUMBER_SPREAD


def build_payload(session_id: str | None = None) -> dict:
    return {
        "action": "opened",
        "repository": {"full_name": DEMO_REPO},
        "pull_request": {
            "number": pr_number_for(session_id),
            "title": DEMO_PR_TITLE,
            "head": {"sha": DEMO_HEAD_SHA},
        },
    }


async def ensure_review_for_session(session_id: str | None, provider: str | None) -> None:
    """POST a signed delivery to our own /webhook. Safe to call on every load.

    The two ContextVars are set around the POST rather than left to the
    caller: the whole point is that they are readable by demo/store.py's
    `enqueue_or_update`, which runs inside this request's own async context
    (the transport below is in-process ASGI -- no socket, no second event
    loop, no lost context). They are reset afterwards so nothing leaks into
    whatever else this request goes on to do.
    """
    body = json.dumps(build_payload(session_id)).encode()
    signature = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    # In-process ASGI, not a real socket: it still runs the whole /webhook
    # route (signature check, dedup, 202, enqueue), but assumes no port and
    # opens no connection, so it works identically under uvicorn and pytest.
    # The import is deliberately lazy -- demo.app imports this module.
    from demo.app import app

    session_token = current_session.set(session_id)
    provider_token = current_provider.set(provider)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
            await client.post(
                "/webhook",
                content=body,
                headers={
                    "X-Hub-Signature-256": signature,
                    "X-GitHub-Delivery": delivery_id_for(session_id),
                    "X-GitHub-Event": "pull_request",
                    "Content-Type": "application/json",
                },
            )
    finally:
        current_session.reset(session_token)
        current_provider.reset(provider_token)
