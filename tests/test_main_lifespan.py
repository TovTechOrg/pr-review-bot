"""Tests for main's lifespan: init_pool, recover_on_startup, and the
dispatcher background task's start/stop.

``ASGITransport`` (used in the existing webhook tests) never fires ASGI
lifespan startup/shutdown events, so this behavior was previously unverified.
We drive ``main.lifespan`` directly as an async context manager instead
of spinning up a real ASGI transport — no new runtime/dev dependency needed.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import main as main
from config import settings
from review_queue import dispatcher, store


@pytest.fixture(autouse=True)
def _env(db, monkeypatch):
    # Ambient GITHUB_WEBHOOK_SECRET (e.g. local dev's .env) is not guaranteed
    # in every test environment (e.g. a fresh git worktree has no untracked
    # .env file) -- lifespan now refuses to start with an empty secret, so
    # every test that isn't specifically exercising that check needs a
    # non-empty stand-in.
    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    # Same reasoning for LLM_PROVIDER, which lost its implicit "gemini"
    # default (design spec 2026-08-18 section 6e) -- lifespan now refuses to
    # start with an empty/unsupported provider too, so every test that isn't
    # specifically exercising that check needs a valid stand-in.
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    # Same reasoning again for GITHUB_TARGET_REPO, which lost its implicit
    # "act on every repo" meaning for an empty value -- "*" is now the
    # explicit spelling of that, and lifespan refuses to start with a plain
    # empty value (2026-09-07).
    monkeypatch.setattr(settings, "github_target_repo", "*")
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


async def _hang_forever() -> None:
    # Stands in for the real infinite dispatcher loop without doing any real
    # work or real sleeping; cancelled cleanly on shutdown.
    await asyncio.Event().wait()


def test_importing_app_main_configures_the_root_logger_for_info_output():
    """ISSUES.md 2026-08-17: the root logger defaults to WARNING when
    unconfigured, which made every logger.info(...) call in the app
    (webhook.py included) permanently unreachable in production --
    confirmed live via Render's Logs API returning no match for a line known
    to have fired. force=True matters specifically: a plain basicConfig() is
    a silent no-op once any handler already exists on root, which is exactly
    what happens under pytest itself (its own logging plugin attaches one) --
    the same failure shape this fix exists to eliminate, from a different
    cause. Not scoped to a fresh import: main is already imported (by
    this file, above) by the time this runs, which is the real-world case
    every other test file in this suite relies on too.
    """
    assert logging.getLogger("webhook").isEnabledFor(logging.INFO)


async def test_lifespan_inits_db_recovers_running_tickets_and_stops_dispatcher(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    # Ambient GitHub App config (e.g. local dev's .env) is not real in CI, so
    # pin installation_id and stub discovery to match it (see
    # test_lifespan_verifies_installation_id_when_already_set below) — this
    # test isn't exercising that verification, just db init/recovery/dispatcher.
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", lambda: 12345)

    # Seed a ticket stuck 'running' (as if the process crashed mid-review),
    # via the real test DB, mirroring what recover_on_startup will see when
    # the real init_pool()/recover_on_startup() run inside lifespan.
    tid = store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha1",
        provider="groq", now="2026-01-01T12:00:00+00:00",
    )
    store.claim_next_due(now="2026-01-01T12:00:01+00:00")
    assert store.get_ticket(tid).status == "running"

    # Capture the task created inside lifespan (it's a local variable there,
    # not exposed on the module) by spying on asyncio.create_task.
    created_tasks = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    async with main.lifespan(main.app):
        # (a) init_pool ran: a fresh insert works without error.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=2, head_sha="sha2",
            provider="groq", now="2026-01-01T12:00:02+00:00",
        )
        # (b) recover_on_startup ran: the previously-'running' ticket is
        # reset to 'pending'.
        assert store.get_ticket(tid).status == "pending"

    # (c) after exiting the context manager, the dispatcher task was
    # cancelled/awaited — no leaked background task.
    assert len(created_tasks) == 1
    assert created_tasks[0].done()
    assert created_tasks[0].cancelled()


async def test_lifespan_verifies_installation_id_when_already_set(monkeypatch):
    """GITHUB_APP_INSTALLATION_ID is required, never guessed on the operator's
    behalf -- but a pinned value is still verified against the App's actual
    installation on every boot, not trusted blindly."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)

    calls = []

    def _fake_discover() -> int:
        calls.append(1)
        return 123456

    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", _fake_discover)

    async with main.lifespan(main.app):
        pass

    assert calls == [1]
    assert settings.github_app_installation_id == 123456


async def test_lifespan_refuses_to_start_without_installation_id(monkeypatch):
    """GITHUB_APP_INSTALLATION_ID must always be configured explicitly -- an
    unset value (0) is a hard startup failure, not a cue to auto-discover
    one on the operator's behalf."""
    monkeypatch.setattr(settings, "github_app_installation_id", 0)

    def _boom() -> int:
        raise AssertionError("must not attempt discovery when unset -- it's a hard failure")

    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", _boom)

    with pytest.raises(RuntimeError, match="GITHUB_APP_INSTALLATION_ID"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_installation_id_does_not_match_discovery(monkeypatch):
    """A pinned id that no longer matches the App's actual installation (e.g.
    it was uninstalled and reinstalled) must fail startup loudly, the same
    as a missing one -- never silently patched to the freshly-discovered
    value."""
    monkeypatch.setattr(settings, "github_app_installation_id", 111)
    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", lambda: 222)

    with pytest.raises(RuntimeError) as exc:
        async with main.lifespan(main.app):
            pass
    message = str(exc.value)
    assert "111" in message
    assert "222" in message


async def test_lifespan_refuses_to_start_without_target_repo(monkeypatch):
    """An empty GITHUB_TARGET_REPO used to silently mean "act on every
    repo"; that's now the explicit, non-empty "*" -- a plain empty value is
    unconfigured, not a deliberate track-all choice, and must be a hard
    startup failure like every other required key."""
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)

    def _boom() -> int:
        raise AssertionError("must not attempt installation discovery before this check")

    monkeypatch.setattr(main.github_app, "discover_and_verify_installation_id", _boom)

    with pytest.raises(RuntimeError, match="GITHUB_TARGET_REPO"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_refuses_to_start_without_llm_provider(monkeypatch):
    """LLM_PROVIDER lost its implicit "gemini" default (design spec
    2026-08-18 section 6e) -- guessing a provider would mean silently
    running (and billing) against one the operator never chose. Checked
    before the webhook-secret check, so a non-empty secret alone isn't
    enough to reach it."""
    monkeypatch.setattr(settings, "llm_provider", "")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    with pytest.raises(RuntimeError) as exc:
        async with main.lifespan(main.app):
            pass
    message = str(exc.value)
    assert "LLM_PROVIDER" in message
    for provider in ("gemini", "groq", "vertex"):
        assert provider in message


async def test_lifespan_fails_loudly_when_webhook_secret_is_empty(monkeypatch):
    """An empty GITHUB_WEBHOOK_SECRET makes verify_signature accept any
    signature (HMAC with an empty key) -- an effective auth bypass. Startup
    must refuse to run rather than silently degrade."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "github_webhook_secret", "")

    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_postgres_is_unreachable(monkeypatch, db_url):
    """Design spec section 11: "If Postgres is unreachable at boot, startup fails
    loudly (correct)". Guards that init_pool()'s diagnostic rewrite did not soften
    that into a warning, and that no dispatcher task is left running."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", lambda: 12345)

    # The autouse _env(db) fixture already opened a pool on the test Postgres.
    store.close_pool()
    monkeypatch.setattr(
        settings, "database_url", "postgresql://u:p@127.0.0.1:1/postgres?connect_timeout=1"
    )
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    created_tasks = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    with pytest.raises(RuntimeError):
        async with main.lifespan(main.app):
            pass

    # init_pool() raised before create_task was reached: no leaked dispatcher.
    assert created_tasks == []

    # This test deliberately leaves store._pool pointed at a dead host --
    # now that the db fixture reuses one pool per worker instead of closing
    # it every test, that leftover pool would otherwise poison every
    # db-marked test that runs afterward in this worker (each blocking for
    # the real 30s _POOL_TIMEOUT_SECONDS -- still 1 here, since this test's
    # own monkeypatch hasn't been torn down yet -- before erroring). First
    # found by running the full `-m db` suite: 87 tests passed, then a
    # cascade of errors starting with the very next one.
    store.close_pool()
    monkeypatch.setattr(settings, "database_url", db_url)
    store.init_pool()
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=999, head_sha="sha-recovery-check",
        provider="groq", now="2026-01-01T12:00:00+00:00",
    )


async def test_lifespan_fails_loudly_when_dashboard_username_is_empty(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_username", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_dashboard_password_is_empty(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_password", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_dashboard_session_secret_is_empty(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_session_secret", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_SESSION_SECRET"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_dashboard_session_secret_is_too_short(monkeypatch):
    """A non-empty but short secret (e.g. a 5-character value) boots fine
    under the empty-check alone, but HS256 with a short key is
    brute-forceable offline by anyone who captures one session cookie."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_session_secret", "short")

    with pytest.raises(RuntimeError, match="DASHBOARD_SESSION_SECRET"):
        async with main.lifespan(main.app):
            pass
