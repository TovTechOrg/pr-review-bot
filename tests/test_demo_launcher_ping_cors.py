"""The launcher lives on GitHub Pages and must be able to READ the launcher
ping endpoint (config.py's demo_launcher_ping_path, NOT "/healthz" -- see
that field's own comment for why) cross-origin to know when the demo has
finished waking. Scoped to that one path: nothing else in the demo,
including "/healthz" itself, is readable from another origin."""

from __future__ import annotations

import httpx
import pytest

from urllib.parse import urlsplit

import main as _main  # noqa: F401  (see tests/test_demo_dashboard.py for why)
import github_app as _real_github_app
import render_client as _real_render_client
import specialists.base as _real_specialists_base
from config import settings
from review_queue import store as _real_store

# Mirrors demo/app.py's own LAUNCHER_ORIGIN derivation. NOT `from demo.app
# import LAUNCHER_ORIGIN` at module level: demo.app's own module body calls
# install_mocks() at import time, permanently rebinding review_queue.store
# for the rest of collection in this xdist worker (see
# tests/test_demo_dashboard.py's demo_chrome_installed fixture docstring for
# the full hazard) -- every import of demo.app in this file is deliberately
# lazy, inside a fixture body, for that reason.
_guide_parts = urlsplit(settings.guide_base_url)
LAUNCHER_ORIGIN = f"{_guide_parts.scheme}://{_guide_parts.netloc}"
PING_PATH = settings.demo_launcher_ping_path


@pytest.fixture(autouse=True)
def _undo_global_rebinding(monkeypatch):
    """Identical to tests/test_demo_dashboard.py's fixture of the same name --
    importing demo.app runs install_mocks() at import time and permanently
    mutates the real, shared module objects via plain setattr."""
    for module in (_real_github_app, _real_store, _real_render_client):
        for name in dir(module):
            if not name.startswith("_"):
                monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(
        _real_specialists_base, "get_provider", _real_specialists_base.get_provider
    )


@pytest.fixture
def demo_env(monkeypatch):
    from config import settings

    for key, value in {
        "GITHUB_WEBHOOK_SECRET": "demo-webhook-secret",
        "GITHUB_APP_INSTALLATION_ID": "1",
        "GITHUB_TARGET_REPO": "*",
        "DASHBOARD_USERNAME": "demo",
        "DASHBOARD_PASSWORD": "demo",
        "DASHBOARD_SESSION_SECRET": "x" * 32,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "github_webhook_secret", "demo-webhook-secret")
    monkeypatch.setattr(settings, "github_app_installation_id", 1)
    monkeypatch.setattr(settings, "github_target_repo", "*")
    monkeypatch.setattr(settings, "dashboard_username", "demo")
    monkeypatch.setattr(settings, "dashboard_password", "demo")
    monkeypatch.setattr(settings, "dashboard_session_secret", "x" * 32)


@pytest.fixture
def demo_chrome_installed():
    """Ensures demo.app's `/api/demo/*` router and cookie-less-bypass
    middleware are present on the shared `main.app` singleton for the
    duration of this one test. See tests/test_demo_dashboard.py's fixture
    of the same name for the full rationale."""
    from starlette.middleware.base import BaseHTTPMiddleware

    from demo import app as demo_app_module
    from demo.app import (
        _allow_launcher_health_polling,
        _demo_request_context,
        app,
        register_demo_static_route,
    )
    from demo.routes import router as demo_router

    app.include_router(demo_router)
    register_demo_static_route(app)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_demo_request_context)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_allow_launcher_health_polling)
    demo_app_module.install_session_redirect_override()
    app.middleware_stack = None
    yield app


async def test_launcher_ping_is_readable_from_the_launcher_origin(demo_env, demo_chrome_installed):
    transport = httpx.ASGITransport(app=demo_chrome_installed)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(PING_PATH)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LAUNCHER_ORIGIN


async def test_healthz_itself_does_not_get_the_cors_header(demo_env, demo_chrome_installed):
    """Regression guard for the 2026-09-17 rename: the CORS allowance moved
    to demo_launcher_ping_path, it did not additionally grow to cover
    "/healthz" too -- least surface exposed, matching the middleware's own
    comment."""
    transport = httpx.ASGITransport(app=demo_chrome_installed)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_no_other_route_becomes_readable_cross_origin(demo_env, demo_chrome_installed):
    """The header is scoped to one path, not applied app-wide.

    Deliberately asserts nothing about the status code: what matters is that
    no non-ping-path response carries the header, and that holds whether the
    route answers 200, redirects to /login, or 404s. Pinning a status here
    would couple this test to the demo's auth behaviour, which it is not
    about.
    """
    transport = httpx.ASGITransport(app=demo_chrome_installed)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/login")
    assert "access-control-allow-origin" not in response.headers
