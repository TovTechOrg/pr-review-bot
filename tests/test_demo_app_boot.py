import pytest

import github_app as _real_github_app
import render_client as _real_render_client
import specialists.base as _real_specialists_base
from config import settings
from providers import catalog as _real_catalog
from providers import credentials as _real_credentials
from providers import vertex_credentials as _real_vertex_credentials
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
    # _app_jwt_client_for is leading-underscore, so the public-only loop
    # above skips it -- but install_mocks() rebinds it too (the
    # github_app-family Guided-setup Validate call), so it needs the same
    # explicit restoration.
    monkeypatch.setattr(
        _real_github_app, "_app_jwt_client_for", _real_github_app._app_jwt_client_for
    )
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
    # Same leak, same fix, for the three modules dashboard/environment.py's
    # Guided-setup/config-table credential-validate calls read: without this,
    # dashboard/tests/test_environment.py runs against demo/providers_mock.py's
    # always-succeeds catalog for the rest of the worker's life.
    for module in (_real_catalog, _real_credentials, _real_vertex_credentials):
        for name in dir(module):
            if not name.startswith("_"):
                monkeypatch.setattr(module, name, getattr(module, name))
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

    import render_client

    assert github_app.fetch_pr_diff.__module__ == "demo.github_app"
    assert store.claim_next_due.__module__ == "demo.store"
    # render_client was the boundary install_mocks() forgot: dashboard/
    # environment.py calls all five of these on the module object, so without
    # the rebinding the Environment panel made a live api.render.com request
    # from a service that holds no API key at all.
    for name in ("find_service_id", "env_vars", "push_env_var", "delete_env_var",
                 "trigger_deploy"):
        assert getattr(render_client, name).__module__ == "demo.render_client", name
    assert specialists.base.get_provider().__class__.__name__ == "MockProvider"


def test_install_mocks_does_not_recurse_never_rebind_functions(demo_env):
    """Regression for the Critical bug found in task-5 review: install_mocks()
    used to reimplement demo_store's rebinding loop directly over
    dir(demo_store) with no exclusion for demo/store.py's _NEVER_REBIND set
    (effective_cooldown/next_cooldown_level/usage_bucket_start -- thin
    passthrough wrappers that call real_store.<name> internally). Rebinding
    them pointed real_store.<name> at a wrapper whose own body called
    real_store.<name> -- which by then *was* the wrapper -- causing
    RecursionError the instant any of them was called (e.g.
    review_queue/dispatcher.py's finalize path calling
    store.effective_cooldown() on every successful review)."""
    from demo import app as demo_app

    demo_app.install_mocks()

    from datetime import datetime, time

    from review_queue import cooldown_config, store

    # effective_cooldown() reads from cooldown_config's own override cache,
    # not from the store directly -- populate it the same way
    # dispatcher.process_next_due() does per ticket (store.get_cooldown_overrides()
    # -> cooldown_config.set_override_cache(...)), otherwise it legitimately
    # returns (None, None, None) and effective_cooldown() TypeErrors on
    # None ** int, unrelated to the recursion bug this test targets.
    cooldown_config.set_override_cache(*store.get_cooldown_overrides())

    # None of these three should raise RecursionError. Real values checked
    # (not just "did not raise") to guard against a future change that makes
    # them silently swallow an exception instead of returning correctly.
    assert isinstance(store.effective_cooldown(1), float)
    assert isinstance(store.next_cooldown_level(1), int)
    assert isinstance(store.usage_bucket_start(datetime(2026, 1, 1), time(0, 0)), datetime)


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


async def test_dispatcher_finalizes_a_ticket_end_to_end_with_mocks_installed(demo_env):
    """The demo's actual purpose: a ticket enqueued against the mocked store
    must be claimable and reviewable by review_queue.dispatcher.process_next_due
    without touching a database or the network, and must reach a terminal
    "done" state -- not get stuck at "running" forever the way the Critical
    RecursionError bug (store.effective_cooldown() called from the finalize
    path) would have caused. Nothing in the original Task 5 test suite
    exercised the dispatcher at all -- only /healthz -- which is exactly how
    that bug shipped undetected."""
    from datetime import datetime, timezone

    from demo import app as demo_app
    from demo import store as demo_store

    demo_app.install_mocks()
    demo_store.reset()
    try:
        from demo.content import DEMO_REPO
        from review_queue import dispatcher, store

        now = datetime.now(timezone.utc)
        ticket_id = store.enqueue_or_update(
            repo_full_name=DEMO_REPO,
            pr_number=1,
            head_sha="deadbeef",
            provider="groq",
            now=now.isoformat(),
        )

        result = await dispatcher.process_next_due(now)

        assert result.action == "ran"
        assert result.ticket_id == ticket_id
        ticket = store.get_ticket(ticket_id)
        assert ticket is not None
        assert ticket.status == "done"
    finally:
        demo_store.reset()
