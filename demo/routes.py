"""Demo-only routes layered over the real dashboard."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from demo.provider import demo_provider_and_model
from demo.session import session_id_for, touch
from demo.trigger import ensure_review_for_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/demo/bootstrap")
async def bootstrap(request: Request) -> JSONResponse:
    """Called by the dashboard on load; makes sure a review exists."""
    session_id = session_id_for(request)
    if session_id is not None:
        touch(session_id)
    requested = request.query_params.get("provider")
    provider, model = demo_provider_and_model(requested)
    await ensure_review_for_session(session_id, provider)

    # Analytics: a structured line per step reached, read from Render's logs
    # during the launch window. No database, no third-party script, and no
    # stored identifier -- `cookieless` is a boolean, not a visitor id.
    logger.info(
        "demo_step step=%s provider=%s cookieless=%s",
        "dashboard_reached", provider, session_id is None,
    )
    return JSONResponse({
        "demo": True,
        "provider": provider,
        "model": model,
        "cookieless": session_id is None,
    })


@router.post("/api/demo/step/{name}")
async def record_step(name: str, request: Request) -> JSONResponse:
    """Records the furthest step a reader reached. Stores nothing."""
    allowed = {"launcher", "wizard_start", "wizard_finish", "review_seen", "cta_clicked"}
    step = name if name in allowed else "unknown"
    logger.info("demo_step step=%s", step)
    return JSONResponse({"recorded": step})
