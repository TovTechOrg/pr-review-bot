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

_SHARED_SESSION = "stateless-shared"


def delivery_id_for(session_id: str | None) -> str:
    basis = session_id or _SHARED_SESSION
    return hashlib.sha256(f"demo-delivery-{basis}".encode()).hexdigest()[:32]


def build_payload() -> dict:
    return {
        "action": "opened",
        "repository": {"full_name": DEMO_REPO},
        "pull_request": {
            "number": DEMO_PR_NUMBER,
            "title": DEMO_PR_TITLE,
            "head": {"sha": DEMO_HEAD_SHA},
        },
    }


async def ensure_review_for_session(session_id: str | None, provider: str | None) -> None:
    """POST a signed delivery to our own /webhook. Safe to call on every load."""
    body = json.dumps(build_payload()).encode()
    signature = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    # In-process ASGI, not a real socket: it still runs the whole /webhook
    # route (signature check, dedup, 202, enqueue), but assumes no port and
    # opens no connection, so it works identically under uvicorn and pytest.
    # The import is deliberately lazy -- demo.app imports this module.
    from demo.app import app

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
