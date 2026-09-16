"""The demo's real failure mode is the flow breaking, not a unit regressing."""

import pytest
from httpx import ASGITransport, AsyncClient

import github_app as _real_github_app
import render_client as _real_render_client
import specialists.base as _real_specialists_base
from config import settings
from review_queue import store as _real_store


@pytest.fixture(autouse=True)
def _undo_global_rebinding(monkeypatch):
    """See tests/test_demo_app_boot.py's identical fixture for the full
    rationale: importing demo.app runs install_mocks() at import time and
    permanently mutates the real, shared github_app/review_queue.store/
    specialists.base module objects via plain setattr -- a leak that
    otherwise survives into every later test in this pytest-xdist worker,
    in either direction (mocked when a later test expects real, or vice
    versa depending on import order). This restores them after each test
    regardless of which test (or import) triggered the rebinding."""
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
def demo_env(monkeypatch):
    # config.settings is a process-wide singleton created lazily on first
    # access (config.py's module __getattr__) and never re-reads os.environ
    # afterward -- by the time this fixture runs, some earlier test in this
    # worker process has almost certainly already forced that first access.
    # monkeypatch.setenv() alone is therefore a no-op for anything the
    # singleton already cached, so this also monkeypatches the settings
    # attributes directly, matching tests/test_demo_app_boot.py's own
    # demo_env fixture (the established convention for this exact problem).
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
    """Re-register demo.app's router, static mount and middleware onto the
    shared main.app singleton for this one test -- see
    tests/test_demo_dashboard.py's identical fixture for the full rationale
    (conftest.py strips all three back off after every test, so a cache-hit
    import of demo.app leaves nothing behind)."""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware

    from demo.app import _demo_request_context, app
    from demo.routes import router as demo_router

    app.include_router(demo_router)
    app.mount(
        "/demo-static",
        StaticFiles(directory=Path(__file__).resolve().parent.parent / "demo" / "static"),
        name="demo-static",
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=_demo_request_context)
    app.middleware_stack = None
    yield app


async def test_two_sessions_get_their_own_review_and_their_own_provider(
    demo_env, demo_chrome_installed
):
    """The regression neither half of the earlier design could have caught.

    Two simulated browsers, each with its own session cookie, each
    bootstrapping with a DIFFERENT ?provider=, driven through the real
    webhook -> dispatcher -> dashboard path. Before this fix:

      * `demo.session.current_session` was never set by any production code
        path, so every review was tagged None and every visitor saw every
        other visitor's reviews;
      * `(repo, pr_number)` was a pair of fixed constants, so both visitors
        collapsed onto ONE shared ticket;
      * the chosen provider never left the bootstrap route -- demo/store.py
        answered every `get_provider_override()` with one module-level
        constant, which the real dispatcher (by its own documented design)
        gates on globally rather than per ticket.

    Each of the three bugs alone is enough to fail this test.
    """
    from datetime import datetime, timezone

    from dashboard import auth
    from demo import app as demo_app
    from demo import session as demo_session
    from demo import store as demo_store
    from review_queue import dispatcher
    from webhook import reset_dedup_cache

    demo_app.install_mocks()
    app = demo_app.app
    demo_store.reset()
    demo_session.reset()
    reset_dedup_cache()

    # Two genuinely different tokens: create_session_token embeds only an exp
    # claim, so two calls in the same second with the same `remember` value
    # produce a byte-identical JWT.
    token_a = auth.create_session_token(remember=False)
    token_b = auth.create_session_token(remember=True)
    assert token_a != token_b

    transport = ASGITransport(app=app)

    async def _bootstrap(token: str, provider: str) -> dict:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={auth.SESSION_COOKIE_NAME: token},
        ) as client:
            response = await client.get(f"/api/demo/bootstrap?provider={provider}")
        assert response.status_code == 200
        return response.json()

    async def _dashboard_reviews(token: str) -> list[dict]:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={auth.SESSION_COOKIE_NAME: token},
        ) as client:
            response = await client.get("/api/dashboard")
        assert response.status_code == 200
        return response.json()["reviews"]

    assert (await _bootstrap(token_a, "vertex"))["provider"] == "vertex"
    assert (await _bootstrap(token_b, "groq"))["provider"] == "groq"

    # Drain the queue exactly as run_forever() would -- serially, one ticket
    # per call, which is the property the "currently processing" slot in
    # demo/store.py relies on.
    actions = []
    for _ in range(4):
        result = await dispatcher.process_next_due(datetime.now(timezone.utc))
        actions.append(result.action)
        if result.action == "idle":
            break
    assert actions.count("ran") == 2, actions

    reviews_a = await _dashboard_reviews(token_a)
    reviews_b = await _dashboard_reviews(token_b)

    assert len(reviews_a) == 1, "a reader must see their own review and nobody else's"
    assert len(reviews_b) == 1
    assert reviews_a[0]["provider"] == "vertex"
    assert reviews_b[0]["provider"] == "groq"
    assert reviews_a[0]["pr_number"] != reviews_b[0]["pr_number"]
    assert "_session" not in reviews_a[0], "the tag is internal, never rendered"


async def test_delivery_to_rendered_review(demo_env):
    """webhook -> ticket -> dispatcher -> orchestrator -> dashboard payload."""
    from demo import app as demo_app
    from demo import store as demo_store
    from demo.trigger import build_payload
    from review_queue import dispatcher
    from datetime import datetime, timezone
    import hashlib
    import hmac
    import json

    # demo.app's module body only runs install_mocks() on the module's
    # FIRST import in this worker process -- if some earlier test already
    # imported demo.app and then its own teardown restored the real
    # github_app/store/specialists.base functions (see
    # tests/test_demo_app_boot.py's _undo_global_rebinding), a later bare
    # `from demo.app import app` reuses the cached module object without
    # re-mocking anything. Calling install_mocks() again here makes this
    # test's mocking state independent of import order/test order.
    demo_app.install_mocks()
    app = demo_app.app

    demo_store.reset()
    body = json.dumps(build_payload()).encode()
    signature = "sha256=" + hmac.new(
        b"demo-webhook-secret", body, hashlib.sha256
    ).hexdigest()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/webhook", content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "e2e-delivery-1",
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )
    assert accepted.status_code == 202

    result = await dispatcher.process_next_due(datetime.now(timezone.utc))
    assert result.action == "ran"

    reviews = demo_store.dashboard_reviews()
    assert len(reviews) == 1
    assert reviews[0]["repo"] == "bot-demo/example-app"
    assert reviews[0]["provider"] in {"gemini", "vertex", "groq"}
    assert reviews[0]["est_cost_usd"] is not None, "unpriced pair renders a blank cost"


async def test_a_replayed_delivery_does_not_review_twice(demo_env):
    from demo import app as demo_app
    from demo import store as demo_store
    from demo.trigger import build_payload
    from webhook import reset_dedup_cache
    import hashlib
    import hmac
    import json

    demo_app.install_mocks()
    app = demo_app.app

    demo_store.reset()
    reset_dedup_cache()
    body = json.dumps(build_payload()).encode()
    signature = "sha256=" + hmac.new(
        b"demo-webhook-secret", body, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": "same-delivery",
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/webhook", content=body, headers=headers)
        second = await client.post("/webhook", content=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    # dashboard_queue_counts() always returns all 7 known ticket statuses as
    # keys (each defaulting to 0), so its length is always 7 regardless of
    # how many tickets exist -- len(...) == 1 could never pass. The total
    # ticket count across all statuses is what actually proves the replay
    # didn't enqueue a second ticket.
    assert sum(demo_store.dashboard_queue_counts().values()) == 1
