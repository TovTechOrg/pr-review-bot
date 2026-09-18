"""Demo-only routes layered over the real dashboard."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import settings
from demo.provider import demo_provider_and_model
from demo.session import SHARED_SESSION_ID, current_session, touch
from demo.trigger import ensure_review_for_session

logger = logging.getLogger(__name__)
router = APIRouter()


# See config.py's demo_launcher_ping_path field comment for why this exists
# as a second endpoint rather than reusing "/healthz" (main.py) -- same
# trivial body, but the launcher's own cross-origin poll is the only caller.
@router.get(settings.demo_launcher_ping_path)
@router.head(settings.demo_launcher_ping_path)
async def launcher_ping() -> dict:
    return {"status": "ok"}


@router.get("/api/demo/bootstrap")
async def bootstrap(request: Request) -> JSONResponse:
    """Called by the dashboard on load; makes sure a review exists.

    The session id comes from the ContextVar demo/app.py's middleware
    already resolved for this request (the dashboard session cookie, or the
    one shared identity every cookie-hostile visitor gets) rather than being
    re-derived from the raw cookie here -- otherwise a cookie-hostile
    visitor, whose synthetic session exists only in that middleware, would
    be indistinguishable from "no session" and lose their own review.
    """
    session_id = current_session.get()
    if session_id is not None:
        touch(session_id)
    requested_provider = request.query_params.get("provider")
    requested_model = request.query_params.get("model")
    provider, model = demo_provider_and_model(requested_provider, requested_model)
    await ensure_review_for_session(session_id, provider, model)

    # Analytics: a structured line per step reached, read from Render's logs
    # during the launch window. No database, no third-party script, and no
    # stored identifier -- `cookieless` is a boolean, not a visitor id.
    cookieless = session_id is None or session_id == SHARED_SESSION_ID
    logger.info(
        "demo_step step=%s provider=%s cookieless=%s",
        "dashboard_reached", provider, cookieless,
    )
    return JSONResponse({
        "demo": True,
        "provider": provider,
        "model": model,
        "cookieless": cookieless,
    })


@router.post("/api/demo/step/{name}")
async def record_step(name: str, request: Request) -> JSONResponse:
    """Records the furthest step a reader reached. Stores nothing."""
    allowed = {"launcher", "wizard_start", "wizard_finish", "review_seen", "cta_clicked"}
    step = name if name in allowed else "unknown"
    logger.info("demo_step step=%s", step)
    return JSONResponse({"recorded": step})
