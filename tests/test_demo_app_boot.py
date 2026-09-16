import pytest

import github_app as _real_github_app
import specialists.base as _real_specialists_base
from config import settings
from review_queue import store as _real_store


@pytest.fixture(autouse=True)
def _undo_global_rebinding(monkeypatch):
    """demo.app.install_mocks() permanently mutates the real, shared
    `github_app`/`review_queue.store`/`specialists.base` module objects via
    plain `setattr` -- exactly as the brief specifies, since a demo process
    (`uvicorn demo.app:app`) never runs alongside the rest of this test
    suite in the same interpreter. Inside this suite it does, and importing
    `demo.app` even once (its own module body calls install_mocks() at
    import time, before `from main import app`) leaves every other test in
    this worker process running against mocked functions for the rest of
    the run -- including a real recursion bug it would otherwise mask
    (review_queue.store.usage_bucket_start rebound to demo.store's
    passthrough wrapper, which calls back into the name it just replaced).

    monkeypatch.setattr(obj, name, getattr(obj, name)) records "restore to
    this exact object" without changing anything now, so whatever
    install_mocks() does to these three modules during the test is undone at
    teardown regardless of which test (or import) triggered it.
    """
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
def demo_env(monkeypatch):
    # config.settings is a process-wide singleton created lazily on first
    # access (config.py's module __getattr__) and never re-reads os.environ
    # afterward -- by the time this fixture runs, some earlier test in this
    # worker process has almost certainly already forced that first access.
    # monkeypatch.setenv() alone is therefore a no-op for anything the
    # singleton already cached; every other test in this suite that needs a
    # specific settings value (e.g. tests/test_main_lifespan.py's `_env`
    # fixture) monkeypatches the settings attribute directly for the same
    # reason, so this follows that same convention rather than env vars.
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


def test_install_mocks_rebinds_every_external_boundary(demo_env):
    from demo import app as demo_app

    demo_app.install_mocks()

    import github_app
    import specialists.base
    from review_queue import store

    assert github_app.fetch_pr_diff.__module__ == "demo.github_app"
    assert store.claim_next_due.__module__ == "demo.store"
    assert specialists.base.get_provider().__class__.__name__ == "MockProvider"


async def test_app_boots_without_a_database_or_network(demo_env, live_operator_apis_allowed):
    # conftest.py's autouse `_quarantine_operator_apis` blanks
    # settings.github_webhook_secret (one of _LIVE_OPERATOR_KEYS) for every
    # test by default, overriding demo_env's monkeypatched env var -- that
    # quarantine exists to stop a test from accidentally reaching real
    # infrastructure, which is exactly what install_mocks() below already
    # guarantees this test cannot do (every external boundary is rebound to
    # an in-memory mock before main.lifespan ever runs). Requesting this
    # fixture is the documented, deliberate opt-out for a test whose own
    # mocking already makes the quarantine redundant.
    from httpx import ASGITransport, AsyncClient

    from demo.app import app, install_mocks

    # demo.app's module body calls install_mocks() once, at first import --
    # a no-op here if this test file's other test (or an xdist worker
    # reusing this process) already imported demo.app, since Python only
    # executes a module body once per process. install_mocks() is
    # idempotent (plain setattr to the same demo functions), so calling it
    # again explicitly guarantees the mocks are active for *this* test
    # regardless of import order or of _undo_global_rebinding's per-test
    # restore above.
    install_mocks()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/healthz")
    assert response.status_code == 200
