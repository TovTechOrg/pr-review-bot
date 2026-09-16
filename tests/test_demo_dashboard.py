"""Task 7: dashboard demo chrome -- fake env panel, demo routes, and the
cookie-less login bypass.
"""

from __future__ import annotations

import pytest

import main as _main  # noqa: F401
import github_app as _real_github_app
import specialists.base as _real_specialists_base
from config import settings
from review_queue import store as _real_store

# Importing plain `main` above, at module import time (during collection),
# is load-bearing for the caplog-based tests below, not decorative: main.py
# calls `logging.basicConfig(level=logging.INFO, force=True)` at its own
# import time, and `force=True` replaces the root logger's handlers
# outright. If `main` (or `demo.app`, which imports it) were first imported
# lazily inside a test function body -- as the brief's own test text does
# with its own `from demo.app import app` line -- that basicConfig call
# would strip out pytest's caplog handler (already attached to root before
# the test body runs) and replace it with a plain stderr StreamHandler,
# silently making caplog.text empty for that test.
#
# Deliberately `import main`, NOT `import demo.app`: demo.app's own module
# body calls install_mocks() at import time, permanently rebinding
# review_queue.store's real public functions (store.init_pool included) to
# demo/store.py's in-memory stand-ins via plain setattr on the shared,
# process-wide module object -- exactly the hazard
# test_demo_app_boot.py's `_undo_global_rebinding` fixture below exists to
# undo, but only at each *test's* teardown, not before collection has even
# finished. Importing demo.app here at collection time (measured directly)
# corrupts `review_queue.store` for every other test file's session in the
# same pytest-xdist worker for the remainder of collection, well before
# this file's own autouse fixture ever gets a chance to run -- surfacing
# nowhere in this file, but as a wave of `RuntimeError: store.init_pool()
# has not been called` failures across dashboard/tests/test_environment.py
# and friends. `main` alone triggers the same basicConfig side effect
# without any of demo.app's store-rebinding side effects.


@pytest.fixture(autouse=True)
def _undo_global_rebinding(monkeypatch):
    """See tests/test_demo_app_boot.py's identical fixture for the full
    rationale: importing demo.app runs install_mocks() at import time and
    permanently mutates the real, shared github_app/review_queue.store/
    specialists.base module objects via plain setattr. This restores them
    after each test regardless of which test (or import) triggered it."""
    for name in dir(_real_github_app):
        if not name.startswith("_"):
            monkeypatch.setattr(_real_github_app, name, getattr(_real_github_app, name))
    for name in dir(_real_store):
        if not name.startswith("_"):
            monkeypatch.setattr(_real_store, name, getattr(_real_store, name))
    monkeypatch.setattr(
        _real_specialists_base, "get_provider", _real_specialists_base.get_provider
    )


@pytest.fixture
def demo_chrome_installed():
    """Ensures demo.app's `/api/demo/*` router and cookie-less-bypass
    middleware are present on the shared `main.app` singleton for the
    duration of this one test.

    demo/app.py registers both at MODULE IMPORT time, unconditionally and
    only once (Python never re-runs a module body on a second import) --
    fine for the real demo process, where nothing else ever touches
    `main.app`. Inside this test suite, though, conftest.py's
    `_restore_main_app_after_demo_app_mutation` autouse fixture strips both
    back off of `main.app` after *every* test (see its docstring for why:
    dozens of other test files import `main.app` directly and assume it is
    pristine, and demo/app.py's own one-time registration would otherwise
    leak the login bypass into every one of them for the rest of the
    worker's life). That means whichever test happens to be the first,
    ever, to import demo.app in a given worker is the only test that would
    see the router/middleware actually present via the import alone --
    every test after it gets a no-op cache-hit import with nothing left to
    show for it, because the previous test's teardown already wiped it.
    Re-applying the same registration explicitly, every time a test needs
    it, decouples "was demo.app ever imported anywhere" from "is the
    behavior it registers present for this test" -- exactly what these
    tests need to be independently reliable regardless of test order.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    from demo.app import _cookieless_bypasses_login, app
    from demo.routes import router as demo_router

    app.include_router(demo_router)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_cookieless_bypasses_login)
    app.middleware_stack = None
    yield app


@pytest.fixture
def demo_env(monkeypatch):
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


async def test_env_panel_values_are_fake_and_masked():
    from demo.render_client import env_vars

    entries = env_vars()
    assert entries
    for entry in entries:
        assert set(entry) >= {"key", "value"}
        assert entry["value"] == "*" * 8


async def test_save_reports_success_without_mutating():
    from demo.render_client import env_vars, update_env_var

    before = env_vars()
    result = update_env_var("GEMINI_API_KEY", "whatever-the-reader-typed")
    assert result["applied"] is True
    assert env_vars() == before


def test_cookieless_request_has_no_session_id():
    from types import SimpleNamespace

    from demo.session import session_id_for

    assert session_id_for(SimpleNamespace(cookies={})) is None


async def test_step_endpoint_logs_a_structured_line_and_stores_nothing(
    caplog, demo_chrome_installed
):
    import logging

    from httpx import ASGITransport, AsyncClient

    from demo.app import app

    transport = ASGITransport(app=app)
    # httpx's own client logs "HTTP Request: ..." at INFO and propagates to
    # root (main.py's logging.basicConfig(level=INFO, force=True) makes the
    # root logger capture it too) -- silenced here so caplog.text reflects
    # only demo.routes's own line, matching what these assertions check.
    # Deliberately NOT a second nested caplog.at_level(): caplog.at_level
    # sets the level on caplog's own SHARED capture handler (not just the
    # named logger), so a second nested call would clobber the first call's
    # handler level and silently drop the very record this test is checking
    # for. logging.getLogger("httpx").setLevel(...) only touches the httpx
    # logger's own level, leaving caplog's handler level alone.
    httpx_logger = logging.getLogger("httpx")
    httpx_orig_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.INFO, logger="demo.routes"):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/demo/step/review_seen")
    finally:
        httpx_logger.setLevel(httpx_orig_level)

    assert response.json() == {"recorded": "review_seen"}
    assert "demo_step step=review_seen" in caplog.text


async def test_unknown_step_names_are_not_echoed_into_the_log(caplog, demo_chrome_installed):
    import logging

    from httpx import ASGITransport, AsyncClient

    from demo.app import app

    transport = ASGITransport(app=app)
    # See test_step_endpoint_logs_a_structured_line_and_stores_nothing above
    # for why httpx's own INFO logging must be silenced (via the httpx
    # logger's own level, not a second nested caplog.at_level) -- without
    # it, httpx's request-log line echoes the raw URL (containing
    # "%3Cinjected%3E", which itself contains "injected") into caplog.text,
    # producing a false failure unrelated to what this test actually checks:
    # that demo.routes itself never echoes an unrecognized step name.
    httpx_logger = logging.getLogger("httpx")
    httpx_orig_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.INFO, logger="demo.routes"):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post("/api/demo/step/%3Cinjected%3E")
    finally:
        httpx_logger.setLevel(httpx_orig_level)

    assert "injected" not in caplog.text
    assert "demo_step step=unknown" in caplog.text


async def test_cookieless_request_still_reaches_a_login_gated_route(
    demo_env, demo_chrome_installed
):
    """The concrete, end-to-end proof the cookie-less bypass middleware
    actually works, not just that session_id_for() returns None in
    isolation (see test_cookieless_request_has_no_session_id above).

    Starlette's Request.cookies/.headers are lazily computed and cached
    per-Request-instance. demo.app's `_cookieless_bypasses_login` middleware
    reads `request.cookies` (caching Starlette's Headers snapshot onto its
    OWN Request object's scope["headers"] list) before mutating
    `request.scope["headers"]` to inject a synthesized session cookie. That
    mutation is invisible to the middleware's own already-cached `request`
    instance, but downstream FastAPI dependency-injection machinery
    (main.py's `Depends(require_session)` gating GET /api/dashboard)
    constructs its OWN fresh Request from that same, by-then-mutated scope
    dict -- so the injected cookie must still be visible there.

    A request with zero cookies sent by the test client must reach a real
    require_session-gated route (GET /api/dashboard, gated in main.py via
    `app.include_router(dashboard_router, dependencies=[Depends(require_session)])`)
    and get a 200, not a 401 -- the exact failure mode this whole feature
    exists to prevent (a cookie-hostile browser, e.g. LinkedIn's in-app
    browser, must never see the login gate in the demo).
    """
    from httpx import ASGITransport, AsyncClient

    from demo.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/dashboard")

    assert response.cookies == {}
    assert response.status_code == 200
    body = response.json()
    assert "stats" in body
