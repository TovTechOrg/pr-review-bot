"""Task 7: dashboard demo chrome -- fake env panel, demo routes, and the
cookie-less login bypass.
"""

from __future__ import annotations

import pytest

import main as _main  # noqa: F401
import github_app as _real_github_app
import render_client as _real_render_client
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
    # install_mocks() rebinds render_client's five network functions too, for
    # the same reason and with the same process-wide blast radius -- without
    # this, dashboard/tests/test_environment.py runs against the demo's fake
    # Render for the rest of the worker's life.
    for name in dir(_real_render_client):
        if not name.startswith("_"):
            monkeypatch.setattr(_real_render_client, name, getattr(_real_render_client, name))
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
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware

    from demo.app import _demo_request_context, app
    from demo.routes import router as demo_router

    app.include_router(demo_router)
    # Same reasoning as the router/middleware above: conftest.py's autouse
    # restore fixture wipes `app.router.routes` back to its pristine,
    # demo-app-unaware snapshot after every test -- a static Mount is just
    # another entry in that same list, so it needs the same per-test
    # re-registration to be reliably present regardless of test order.
    app.mount(
        "/demo-static",
        StaticFiles(directory=Path(__file__).resolve().parent.parent / "demo" / "static"),
        name="demo-static",
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=_demo_request_context)
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


async def test_env_panel_values_are_fake_and_match_the_real_module_shape():
    """`env_vars` must take a service id and return key -> value, exactly like
    the real render_client -- dashboard/environment.py iterates
    `values.items()` and probes it with `var in values`. The demo's earlier
    zero-argument, list-returning version would have TypeError'd the moment
    it was actually installed."""
    import inspect

    import render_client as real_render_client
    from demo import render_client as demo_render_client

    assert (
        inspect.signature(demo_render_client.env_vars)
        == inspect.signature(real_render_client.env_vars)
    )
    values = demo_render_client.env_vars(demo_render_client.find_service_id())
    assert isinstance(values, dict)
    assert values and all(isinstance(v, str) for v in values.values())


async def test_save_reports_success_without_mutating():
    from demo import render_client as demo_render_client

    service_id = demo_render_client.find_service_id()
    before = demo_render_client.env_vars(service_id)
    assert demo_render_client.push_env_var(service_id, "GEMINI_API_KEY", "typed") is None
    assert demo_render_client.trigger_deploy(service_id)
    assert demo_render_client.env_vars(service_id) == before


async def test_environment_panel_answers_without_a_network_call(
    demo_env, demo_chrome_installed
):
    """The regression that matters: demo/render_client.py existed but was
    never installed, so this endpoint called the REAL module, made a live
    HTTPS request to api.render.com with no API key, and 500'd. Hitting the
    route through the real ASGI app with a real session is the only thing
    that proves installation -- importing demo.render_client directly passed
    the whole time the panel was broken."""
    from httpx import ASGITransport, AsyncClient

    from dashboard import auth
    from demo import app as demo_app

    demo_app.install_mocks()
    transport = ASGITransport(app=demo_app.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    ) as client:
        response = await client.get("/api/environment/render")

    assert response.status_code == 200
    body = response.json()
    keys = {row["key"] for row in body["vars"]}
    assert "DATABASE_URL" in keys
    # The masking itself is the dashboard's job (dashboard.html's
    # maskedValue); what matters here is that the protected flag the real
    # module drives is present and true for a protected key.
    assert next(r for r in body["vars"] if r["key"] == "DATABASE_URL")["protected"] is True
    assert set(body["available_key_slots"]) == {"gemini", "groq", "vertex"}


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

    A request with zero cookies AND the `?cookieless=1` marker that
    demo/static/demo.js only adds once it has PROVEN the probe cookie did not
    stick must reach a real require_session-gated route (GET /api/dashboard,
    gated in main.py via `app.include_router(dashboard_router,
    dependencies=[Depends(require_session)])`) and get a 200, not a 401 --
    the exact failure mode this whole feature exists to prevent (a
    cookie-hostile browser, e.g. LinkedIn's in-app browser, must never see
    the login gate in the demo).
    """
    from httpx import ASGITransport, AsyncClient

    from demo.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/dashboard?cookieless=1")

    assert response.status_code == 200
    body = response.json()
    assert "stats" in body


async def test_a_cookie_capable_first_visit_still_sees_the_login_page(
    demo_env, demo_chrome_installed
):
    """Regression for the Critical finding: the bypass used to fire for ANY
    request without the session cookie -- including a normal browser's very
    first request ever, which is indistinguishable from "has not logged in
    yet". The consequence was that the login screen (an explicit step of the
    design's reader path, running the real auth check) never rendered for
    anyone, on any browser, ever.

    A cookie-capable client (one AsyncClient instance, persisting cookies
    across requests, exactly like a browser) must be redirected to /login and
    must still be redirected on a second visit, once it IS carrying the probe
    cookie the server handed it.
    """
    from httpx import ASGITransport, AsyncClient

    from dashboard.auth import SESSION_COOKIE_NAME
    from demo.app import PROBE_COOKIE_NAME, app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        first = await client.get("/")
        login_page = await client.get("/login")
        second = await client.get("/")

        assert first.status_code == 303
        assert first.headers["location"] == "/login"
        assert login_page.status_code == 200
        # The probe cookie was handed out and kept -- this client is not
        # cookie-hostile, and the server can now tell the difference.
        assert PROBE_COOKIE_NAME in client.cookies
        assert SESSION_COOKIE_NAME not in client.cookies
        assert second.status_code == 303
        assert second.headers["location"] == "/login"


async def test_the_bypass_is_refused_once_the_browser_has_proven_it_keeps_cookies(
    demo_env, demo_chrome_installed
):
    """`?cookieless=1` is a hint from the page, not an authority: a client
    that demonstrably stores cookies (it is carrying the probe) must still be
    sent to the login page even if the parameter is present -- otherwise the
    marker would be a plain "skip login" switch anyone could paste into a
    URL."""
    from httpx import ASGITransport, AsyncClient

    from demo.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        await client.get("/login")  # collects the probe cookie
        response = await client.get("/?cookieless=1")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_demo_static_mount_serves_demo_js(demo_chrome_installed):
    """Concrete proof the static mount is actually wired, not just that
    demo/static/demo.js exists on disk. A task reviewer found that
    demo.js -- banner injection, readonly login prefill, the bootstrap
    fetch -- was created but never mounted or referenced anywhere, so the
    whole visible point of this task's chrome never ran in a real page
    load. This hits the ASGI app the same way a browser's <script src=...>
    tag would."""
    from httpx import ASGITransport, AsyncClient

    from demo.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/demo-static/demo.js")

    assert response.status_code == 200
    assert "text/javascript" in response.headers["content-type"] or (
        "javascript" in response.headers["content-type"]
    )
    assert "demoBanner" in response.text
    assert "prefillLogin" in response.text


class _FakeReview:
    provider = "groq"
    model = "llama-3.3-70b-versatile"
    total_elapsed_ms = 10
    total_tokens_in = 1
    total_tokens_out = 1
    est_cost_usd = 0.0001
    results: list = []


async def test_the_sweep_evicts_a_session_and_everything_it_owns():
    """Finding 4: sweep()/is_known() were only ever called from tests, so
    `_tickets`/`_reviews`/`_comments`/`_last_seen` grew for the life of the
    process behind an unauthenticated, publicly loopable endpoint."""
    from demo import app as demo_app
    from demo import github_app as demo_github_app
    from demo import session as demo_session
    from demo import store as demo_store

    demo_session.reset()
    demo_store.reset()
    demo_github_app.reset()

    demo_session.touch("gone", now=0.0)
    token = demo_session.current_session.set("gone")
    try:
        demo_store.enqueue_or_update(
            repo_full_name="bot-demo/example-app", pr_number=11,
            head_sha="abc", provider="groq", now="2026-09-16T00:00:00+00:00",
        )
        demo_store.record_review(
            "bot-demo/example-app", 11, _FakeReview(), None, "2026-09-16T00:00:00+00:00", 0,
        )
    finally:
        demo_session.current_session.reset(token)
    demo_github_app.upsert_comment("bot-demo/example-app", 11, "body")

    assert demo_store.dashboard_queue_counts()["pending"] == 1

    # A real, monotonic-clock-independent expiry: touch() recorded now=0.0,
    # so any current monotonic reading is already well past the TTL.
    dropped = await demo_app.sweep_once()

    assert dropped == 2
    assert demo_session.is_known("gone") is False
    assert demo_store.dashboard_queue_counts()["pending"] == 0
    assert demo_store.dashboard_stats()["total_reviews"] == 0


def test_the_provider_parameter_survives_the_login_redirect_and_bootstrap_waits():
    """Finding 5, both halves, checked against the shipped assets.

    The design's ordering is explicit: the reader arrives with ?provider=
    BEFORE authenticating, and the review that reports it is not generated
    until after. login.html dropped the query string on its success redirect
    (`window.location.href = "/"`), and demo.js fired the bootstrap from
    DOMContentLoaded on the login page itself -- pre-auth, and with a
    provider that the post-login page would then have lost anyway.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    login = (root / "dashboard" / "static" / "login.html").read_text(encoding="utf-8")
    demo_js = (root / "demo" / "static" / "demo.js").read_text(encoding="utf-8")

    assert 'window.location.href = "/" + window.location.search;' in login
    assert 'window.location.href = "/";' not in login
    # The bootstrap call is guarded by the login-page check that precedes it.
    bootstrap_at = demo_js.index('fetch("/api/demo/bootstrap"')
    guard_at = demo_js.index("if (isLoginPage()) return;")
    assert guard_at < bootstrap_at
