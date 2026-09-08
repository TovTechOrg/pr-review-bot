"""Dispatcher step logic: burst (RPM defer + later run) and daily-wall defer.

Tests drive process_next_due(now) directly with an injected clock and stubbed
attempt_review — the infinite run_forever loop is a thin wrapper and is not
unit-tested. Uses the shared Postgres test harness and a cleared blocked_until map.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import settings
from providers import active
from review_queue import (
    cooldown_config,
    dispatcher,
    dispatcher_tuning_config,
    store,
    usage_cap_config,
)
import orchestrator as orchestrator

# Captured before the _env fixture below stubs store.get_cooldown_overrides
# for the rest of this file -- lets one test (which exercises the real
# DB-read wiring) restore the genuine function instead of the stub.
_REAL_GET_COOLDOWN_OVERRIDES = store.get_cooldown_overrides

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# NOW is 2026-01-01T12:00Z and the reset defaults to 04:00 UTC, so the
# current usage bucket started at 04:00 today and next resets at 04:00 on
# the 2nd -- 16 hours out.
CAP_RESET_AT = datetime(2026, 1, 2, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(db, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "groq")
    # A hard-failure path now reactively re-verifies the installation id
    # (ISSUES.md 2026-08-21) -- default to "still matches" so tests not
    # specifically exercising that check aren't tripped by a real GitHub
    # App JWT call. Dedicated tests below override this.
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(
        dispatcher.github_app, "discover_and_verify_installation_id", lambda expected: expected
    )
    dispatcher.reset_blocked_until()
    active.reset_override_cache()
    # The 9 tuning knobs are DB-only, no env fallback (Task 5/6) --
    # process_next_due's own refresh calls store.get_dispatcher_tuning_config,
    # which would return all-None values against the `db` fixture's truncated
    # runtime_config otherwise. Stubbed here to Settings' own defaults so
    # every test in this file gets the same real values the pre-refactor
    # settings.dispatcher_* reads used to provide, without needing to seed
    # a real runtime_config row per test.
    monkeypatch.setattr(
        dispatcher.store,
        "get_dispatcher_tuning_config",
        lambda: {
            "llm_request_timeout_seconds": settings.llm_request_timeout_seconds,
            "dispatcher_default_retry_after_seconds": (
                settings.dispatcher_default_retry_after_seconds
            ),
            "dispatcher_failure_base_backoff_seconds": (
                settings.dispatcher_failure_base_backoff_seconds
            ),
            "dispatcher_failure_max_backoff_seconds": (
                settings.dispatcher_failure_max_backoff_seconds
            ),
            "dispatcher_max_failure_attempts": settings.dispatcher_max_failure_attempts,
            "dispatcher_max_notice_post_attempts": settings.dispatcher_max_notice_post_attempts,
            "dispatcher_min_retry_after_seconds": settings.dispatcher_min_retry_after_seconds,
            "dispatcher_backoff_jitter_seconds": settings.dispatcher_backoff_jitter_seconds,
            "dispatcher_notice_sweep_batch_size": settings.dispatcher_notice_sweep_batch_size,
        },
    )
    # cooldown_config/usage_cap_config/review_draft_config also lost their
    # env-fallback tier (Task 6) -- stubbed here the same way, reading
    # `settings` live at call time (not snapshotted) so existing test bodies
    # that monkeypatch e.g. settings.key_usage_token_cap per-test keep
    # working unchanged; a test exercising the refresh-failure path itself
    # re-monkeypatches these below the fixture, which still wins.
    # test_claimed_ticket_uses_the_db_cooldown_override specifically
    # restores the REAL store.get_cooldown_overrides (via _REAL_GET_COOLDOWN_OVERRIDES
    # below) to exercise the actual DB-read wiring this stub otherwise shadows.
    monkeypatch.setattr(
        dispatcher.store,
        "get_cooldown_overrides",
        lambda: (
            settings.dispatcher_rereview_cooldown_seconds,
            settings.dispatcher_rereview_cooldown_max_seconds,
            settings.dispatcher_rereview_cooldown_factor,
        ),
    )
    monkeypatch.setattr(
        dispatcher.store,
        "get_usage_cap_overrides",
        lambda: (settings.key_usage_token_cap, settings.key_usage_reset_time_utc.isoformat()),
    )
    monkeypatch.setattr(
        dispatcher.store, "get_review_draft_override", lambda: settings.review_draft_prs
    )
    yield
    dispatcher.reset_blocked_until()
    active.reset_override_cache()
    dispatcher_tuning_config.reset_override_cache()


@pytest.fixture(autouse=True)
def _clean_cooldown_cache():
    cooldown_config.reset_override_cache()
    yield
    cooldown_config.reset_override_cache()


@pytest.fixture(autouse=True)
def _clean_key_index_cache():
    from providers import key_index

    key_index.reset_override_cache()
    yield
    key_index.reset_override_cache()


@pytest.fixture(autouse=True)
def _caps_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    yield


@pytest.fixture(autouse=True)
def _clean_usage_cap_cache():
    usage_cap_config.reset_override_cache()
    yield
    usage_cap_config.reset_override_cache()


def _enqueue(pr, now=NOW):
    return store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha", provider="groq",
        now=now.isoformat(),
    )


def _stub_comments(monkeypatch):
    posted = []

    def fake_upsert(repo, pr, body, comment_id=None):
        posted.append((pr, body, comment_id))
        return SimpleNamespace(id=comment_id)

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", fake_upsert)
    return posted


def _set_comment_id(db_exec, tid, comment_id):
    db_exec("UPDATE tickets SET comment_id = %s WHERE id = %s", (comment_id, tid))


def _record_usage(db_exec, tokens=0, cost=0.0, provider="groq", key_index=0, created_at=None):
    """Insert one completed-review row directly. Raw SQL rather than
    store.record_review so this file needn't build a whole ReviewResult; the
    `results` JSONB is an inline SQL literal so no json adapter is needed."""
    db_exec(
        "INSERT INTO reviews (repo_full_name, pr_number, provider, model, comment_id, "
        "created_at, total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, "
        "results, key_index) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb,%s)",
        ("owner/repo", 1, provider, "m", None, created_at or NOW.isoformat(), 1,
         tokens, 0, cost, key_index),
    )


async def test_idle_when_no_tickets(monkeypatch):
    _stub_comments(monkeypatch)
    result = await dispatcher.process_next_due(NOW)
    assert result.action == "idle"


async def test_completed_ticket_runs_and_marks_done(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=1)

    async def fake_attempt(repo, pr, comment_id=None):
        review = type("R", (), {})()
        return orchestrator.ReviewCompleted(review=review)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_skipped_ticket_is_discarded_on_an_empty_diff(monkeypatch):
    """ReviewSkipped (an empty diff) must leave no comment and no ticket
    trace behind -- ISSUES.md's 'Empty diffs still fan out all 3
    specialists' gap."""
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=1)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewSkipped()

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "skipped"
    assert store.get_ticket(tid) is None
    assert posted == []


async def test_skipped_ticket_rearms_pending_when_a_push_landed_mid_flight(monkeypatch):
    """A push that arrives while an empty-diff ticket is still being
    processed might carry real content -- it must not be lost when the
    ticket is discarded."""
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=1)

    async def fake_attempt(repo, pr, comment_id=None):
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq",
            now=NOW.isoformat(),
        )
        return orchestrator.ReviewSkipped()

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "skipped"
    t = store.get_ticket(tid)
    assert t is not None
    assert t.status == "pending"
    assert t.head_sha == "sha2"


async def test_rate_limited_ticket_defers_posts_placeholder_and_blocks(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=2)
    _set_comment_id(db_exec, tid, 202)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=30)).isoformat()
    assert posted and posted[0][0] == 2            # placeholder posted on PR 2
    assert posted[0][2] == 202                     # threaded comment_id preserved
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=30)


async def test_rate_limited_outcome_placeholder_persists_a_recreated_comment_id(
    monkeypatch, db_exec
):
    """The bot's comment was deleted: upsert_comment creates a fresh one with
    a different id than what was threaded into the call -- that new id must
    be persisted, not silently dropped."""
    posted = []

    def fake_upsert(repo, pr, body, comment_id=None):
        posted.append((pr, body, comment_id))
        return SimpleNamespace(id=999)

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", fake_upsert)
    tid = _enqueue(pr=2)
    _set_comment_id(db_exec, tid, 202)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    await dispatcher.process_next_due(NOW)
    assert posted[0][2] == 202                       # old id threaded into the call
    assert store.get_ticket(tid).comment_id == 999   # new id from GitHub persisted


async def test_blocked_provider_defers_without_calling_attempt(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=3)
    _set_comment_id(db_exec, tid, 303)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert called == []                            # never fired a doomed call
    assert posted and "rate limit" in posted[0][1].lower()
    assert posted[0][2] == 303                     # threaded comment_id preserved


async def test_blocked_provider_placeholder_persists_a_recreated_comment_id(monkeypatch, db_exec):
    posted = []

    def fake_upsert(repo, pr, body, comment_id=None):
        posted.append((pr, body, comment_id))
        return SimpleNamespace(id=888)

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", fake_upsert)
    tid = _enqueue(pr=3)
    _set_comment_id(db_exec, tid, 303)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    async def fake_attempt(repo, pr, comment_id=None):
        raise AssertionError("must not be called while blocked")

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    await dispatcher.process_next_due(NOW)
    assert posted[0][2] == 303
    assert store.get_ticket(tid).comment_id == 888


async def test_first_hard_failure_defers_with_backoff_not_terminal(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=5)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("github api exploded")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"          # retryable, NOT terminal
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()  # base backoff


async def test_hard_stop_marks_failed_and_posts_failure_comment(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)  # first failure is terminal
    tid = _enqueue(pr=8)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("still broken")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 8
    assert "could not be completed" in posted[0][1].lower()


async def test_hard_failure_checks_installation_validity_before_backing_off(monkeypatch):
    """Every hard failure reactively re-verifies the installation id, not
    just the terminal one -- fast detection matters more than checking
    once at the end of a slow backoff cycle."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    tid = _enqueue(pr=95)

    calls = []
    monkeypatch.setattr(
        dispatcher.github_app, "discover_and_verify_installation_id",
        lambda expected: calls.append(expected) or expected,
    )

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"           # ordinary backoff still applies
    assert calls == [12345]                       # installation id from the autouse _env fixture
    assert store.get_ticket(tid).status == "retrying"


async def test_hard_failure_terminates_the_process_when_installation_confirmed_invalid(
    monkeypatch,
):
    """A confirmed-dead installation (uninstalled, or reinstalled under a new
    id) must terminate the process rather than silently keep going under a
    stale identity -- an unhandled exception in a background task would
    otherwise just be logged and dropped, not actually fatal."""
    import github_app

    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    _enqueue(pr=96)

    def _confirmed_gone(expected):
        raise github_app.AppNotInstalledError("no installations")

    monkeypatch.setattr(
        dispatcher.github_app, "discover_and_verify_installation_id", _confirmed_gone
    )

    exits = []
    monkeypatch.setattr(dispatcher.os, "_exit", lambda code: exits.append(code))

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    await dispatcher.process_next_due(NOW)
    assert exits == [1]


async def test_hard_failure_terminates_on_an_installation_id_mismatch(monkeypatch):
    """_installation_confirmed_invalid's third path: a plain RuntimeError
    raised directly (no `from exc`) by discover_and_verify_installation_id on
    an id mismatch, or by discover_installation_id_for_app on an ambiguous
    multiple-installations result. Only AppNotInstalledError and a
    GithubException-chained RuntimeError were exercised through
    process_next_due before this test -- a regression that accidentally
    chained the mismatch raise (`raise ... from exc`) would silently stop
    terminating the process here and nothing would catch it."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    _enqueue(pr=98)

    def _mismatch(expected):
        raise RuntimeError(f"installation id mismatch: expected {expected}")

    monkeypatch.setattr(dispatcher.github_app, "discover_and_verify_installation_id", _mismatch)

    exits = []
    monkeypatch.setattr(dispatcher.os, "_exit", lambda code: exits.append(code))

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    await dispatcher.process_next_due(NOW)
    assert exits == [1]


async def test_hard_failure_does_not_terminate_on_an_ambiguous_installation_check_failure(
    monkeypatch,
):
    """A transient failure of the verification call itself (e.g. the same
    GitHub-wide outage that likely caused the original review to fail) must
    never be mistaken for a confirmed-dead installation."""
    from github import GithubException

    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    tid = _enqueue(pr=97)

    def _transient(expected):
        try:
            raise GithubException(502, {"message": "boom"}, None)
        except GithubException as e:
            raise RuntimeError("installation lookup failed") from e

    monkeypatch.setattr(
        dispatcher.github_app, "discover_and_verify_installation_id", _transient
    )

    exits = []
    monkeypatch.setattr(dispatcher.os, "_exit", lambda code: exits.append(code))

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert exits == []
    assert result.action == "deferred"            # ordinary backoff, not treated as fatal
    assert store.get_ticket(tid).status == "retrying"


async def test_rate_limited_zero_retry_after_is_floored(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_min_retry_after_seconds", 1.0)
    tid = _enqueue(pr=9)

    async def rl(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=0.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    await dispatcher.process_next_due(NOW)
    t = store.get_ticket(tid)
    assert t.not_before == (NOW + timedelta(seconds=1)).isoformat()   # floored, not now+0
    assert t.attempts == 0                                            # RL not counted
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=1)


async def test_push_during_running_triggers_one_cooldown_re_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(pr=10)

    async def attempt_then_push(repo, pr, comment_id=None):
        # A push lands mid-review -> dirty flag on the running ticket.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=10, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    t = store.get_ticket(tid)
    assert t.status == "deferred"                                     # re-armed, not done
    assert t.not_before == (NOW + timedelta(seconds=300)).isoformat()  # at cooldown

    # During the cooldown wait: nothing due, and NO placeholder churn.
    posted.clear()
    assert (await dispatcher.process_next_due(NOW + timedelta(seconds=60))).action == "idle"
    assert posted == []

    # After cooldown: the re-review runs.
    async def ok(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    result = await dispatcher.process_next_due(NOW + timedelta(seconds=300))
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_dispatcher_escalates_cooldown_on_churn_completion(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(pr=30)
    db_exec("UPDATE tickets SET cooldown_level = 1 WHERE id = %s", (tid,))

    async def attempt_then_push(repo, pr, comment_id=None):
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=30, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=600)).isoformat()   # effective_cooldown(1)
    assert t.cooldown_level == 2                                        # next_cooldown_level(1)
    assert posted == []


async def test_push_during_running_then_deferred_run_does_not_survive_to_next_success(monkeypatch):
    """A push mid-run sets the dirty flag, but if THAT run gets deferred
    (rate-limited here) instead of completing, the flag must not survive to
    the later successful run -- claim_next_due clears it on claim, so the
    flag from the earlier push is considered satisfied by the run that is
    about to happen. Regression test for the stale-flag bug (Finding 1)."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(pr=11)

    async def attempt_then_push_then_rate_limited(repo, pr, comment_id=None):
        # A push lands mid-review -> dirty flag set on the running ticket.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=11, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        # But THIS attempt itself gets rate-limited (deferred, not completed).
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push_then_rate_limited)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=30)).isoformat()

    # The next claim (once due) must clear the stale flag at claim time, so
    # a successful run does not spuriously re-arm for an extra re-review.
    posted.clear()

    async def ok(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    result = await dispatcher.process_next_due(NOW + timedelta(seconds=30))
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"   # NOT "deferred" for a bogus re-review


async def test_blocked_gate_uses_current_settings_provider_not_stale_ticket_provider(monkeypatch):
    """The ticket was enqueued under provider 'groq' (see _enqueue), but the
    _blocked_until gate must key off settings.llm_provider (the CURRENT
    provider actually used by attempt_review), which here we set to a
    different name to simulate LLM_PROVIDER having changed with a ticket
    still in flight."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    _enqueue(pr=7)  # ticket.provider == "groq" (stale, from _enqueue helper)
    dispatcher._blocked_until["gemini"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert called == []  # gated on the current provider's block, not the stale one
    assert posted and "rate limit" in posted[0][1].lower()


def _reviewed_then_pushed(pr, monkeypatch):
    """A ticket that HAS a completed review (last_reviewed_at set) and a pending
    re-review queued by a later push (cooldown 0 -> immediately claimable).

    cooldown_config is DB-only, no env fallback (Task 6) -- store.enqueue_or_update
    (called below) reads the cache directly, not through process_next_due's
    own refresh, so the cache is set here rather than via a settings monkeypatch.
    """
    cooldown_config.set_override_cache(0.0, 3600.0, 2.0)
    tid = _enqueue(pr=pr)
    store.claim_next_due(NOW.isoformat())
    store.finalize_review(
        tid, now=NOW.isoformat(), rereview_not_before=NOW.isoformat(), rereview_cooldown_level=0
    )
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha2",
        provider="groq", now=NOW.isoformat(),
    )
    return tid


async def test_gate_does_not_overwrite_good_review_with_placeholder(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(20, monkeypatch)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    async def fake_attempt(repo, pr, comment_id=None):
        raise AssertionError("attempt_review must not run while blocked")

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # good review preserved; no placeholder posted


async def test_rate_limited_outcome_does_not_overwrite_good_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(21, monkeypatch)

    async def rl(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # no placeholder over the good review


async def test_rate_limited_outcome_then_sweep_posts_schedule_notice(monkeypatch):
    """End-to-end path for the second gap the feature closed: a ticket with a
    visible good review that gets rate-limited on its next attempt must not
    stay fully silent -- the following post_pending_notices sweep should pick
    up exactly the row shape process_next_due's RateLimited path produces and
    post a schedule notice for it."""
    posted = _stub_comments(monkeypatch)
    notices = _stub_append_schedule(monkeypatch)
    tid = _reviewed_then_pushed(23, monkeypatch)

    async def rl(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert posted == []  # no placeholder over the good review

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert notices and notices[0][0] == 23
    assert store.get_ticket(tid).notice_not_before == store.get_ticket(tid).not_before


def _stub_footnotes(monkeypatch):
    appended = []

    def fake_append(repo, pr, footnote, comment_id=None):
        appended.append((pr, footnote, comment_id))
        return SimpleNamespace(id=comment_id)

    monkeypatch.setattr(dispatcher.github_app, "append_review_footnote", fake_append)
    return appended


def _stub_clear_schedule(monkeypatch):
    cleared = []

    def fake_clear(repo, pr, comment_id=None):
        cleared.append((pr, comment_id))
        return SimpleNamespace(id=comment_id)

    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice", fake_clear)
    return cleared


async def test_terminal_failure_appends_footnote_when_good_review_exists(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _reviewed_then_pushed(22, monkeypatch)
    _set_comment_id(db_exec, tid, 2222)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert appended and appended[0][0] == 22   # footnote appended
    assert appended[0][2] == 2222               # threaded comment_id preserved
    assert posted == []                         # good review NOT overwritten
    t = store.get_ticket(tid)
    assert t.comment_id == 2222                 # unchanged id re-persisted, not dropped
    assert t.last_reviewed_at is not None       # still visible -- nothing was lost


async def test_terminal_failure_footnote_confirmed_loss_clears_visible_review(
    monkeypatch, db_exec
):
    """The bot's comment was deleted: append_review_footnote has to create a
    fresh one with a different id than what was on file. That's confirmed
    content loss -- the new id must be persisted AND last_reviewed_at cleared
    so later scheduling/placeholder decisions stop believing a review is
    still visible."""
    appended = []

    def fake_append(repo, pr, footnote, comment_id=None):
        appended.append((pr, footnote, comment_id))
        return SimpleNamespace(id=6666)

    monkeypatch.setattr(dispatcher.github_app, "append_review_footnote", fake_append)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _reviewed_then_pushed(27, monkeypatch)
    _set_comment_id(db_exec, tid, 2727)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert appended[0][2] == 2727                # old id threaded into the call
    t = store.get_ticket(tid)
    assert t.comment_id == 6666                  # new id from GitHub persisted
    assert t.last_reviewed_at is None            # visible-review flag honestly cleared


async def test_terminal_failure_overwrites_when_no_good_review(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _enqueue(pr=24)  # fresh: last_reviewed_at is None
    _set_comment_id(db_exec, tid, 2424)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 24        # overwrite via upsert_comment
    assert "could not be completed" in posted[0][1].lower()
    assert posted[0][2] == 2424                  # threaded comment_id preserved
    assert appended == []                        # no footnote when nothing to preserve
    assert store.get_ticket(tid).comment_id == 2424  # persisted, not just threaded


async def test_terminal_notice_post_failure_defers_instead_of_stranding(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=25)  # fresh -> overwrite path

    def boom_post(repo, pr, body, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"           # NOT failed (visibility guaranteed first)
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()


async def test_repeated_notice_post_failure_eventually_goes_terminal(monkeypatch):
    """Regression test for the unbounded-retry-loop finding: if the terminal
    notice itself keeps failing to post, forever, the ticket must eventually
    give up and go 'failed' rather than looping in 'retrying' indefinitely.
    """
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_max_notice_post_attempts", 3)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_failure_max_backoff_seconds", 300.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=26)  # fresh -> overwrite path (upsert_comment)

    def boom_post(repo, pr, body, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    now = NOW
    result = None
    for _ in range(20):  # plenty more than the notice-post ceiling
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        result = await dispatcher.process_next_due(now)
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        assert t.status == "retrying"
        # Advance past not_before so the next iteration can claim it again.
        now = datetime.fromisoformat(t.not_before) + timedelta(seconds=1)

    final = store.get_ticket(tid)
    assert final.status == "failed"          # terminal reached, not looping forever
    assert result.action == "failed"


async def test_notice_post_ceiling_makes_exactly_max_notice_post_attempts_tries(monkeypatch):
    """dispatcher_max_notice_post_attempts=3 must mean exactly 3 notice-post
    attempts, not dispatcher_max_notice_post_attempts + 1 (the off-by-one this
    is a regression test for: notice_post_ceiling used to be
    dispatcher_max_failure_attempts + dispatcher_max_notice_post_attempts,
    which let one extra retry through)."""
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_max_notice_post_attempts", 3)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_failure_max_backoff_seconds", 300.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=27)  # fresh -> overwrite path (upsert_comment)

    post_attempts = []

    def boom_post(repo, pr, body, comment_id=None):
        post_attempts.append(1)
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    now = NOW
    for _ in range(10):
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        await dispatcher.process_next_due(now)
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        now = datetime.fromisoformat(t.not_before) + timedelta(seconds=1)

    assert store.get_ticket(tid).status == "failed"
    assert len(post_attempts) == 3


async def test_daily_wall_defers_then_runs_after_reset(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=4)

    async def rate_limited(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=6 * 3600)

    monkeypatch.setattr(dispatcher, "attempt_review", rate_limited)
    await dispatcher.process_next_due(NOW)
    assert store.get_ticket(tid).status == "deferred"

    # Before reset: nothing is due.
    assert (await dispatcher.process_next_due(NOW + timedelta(hours=1))).action == "idle"

    # After reset: blocked_until has passed, ticket runs.
    async def ok(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    later = NOW + timedelta(hours=7)
    result = await dispatcher.process_next_due(later)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_completed_review_persists_returned_comment_id(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=60)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=4242)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).comment_id == 4242


async def test_attempt_review_is_called_with_ticket_comment_id(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=61)
    db_exec("UPDATE tickets SET comment_id = 909 WHERE id = %s", (tid,))
    seen = {}

    async def fake_attempt(repo, pr, comment_id=None):
        seen["comment_id"] = comment_id
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=909)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    await dispatcher.process_next_due(NOW)
    assert seen["comment_id"] == 909   # ticket's stored id passed into attempt_review


async def test_sustained_churn_escalates_then_plateaus(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(pr=40)

    async def complete_with_push(repo, pr, comment_id=None):
        # a push lands during every review -> dirty flag -> re-arm each cycle
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=40, head_sha="s",
            provider="groq", now="2026-01-01T00:00:00+00:00",
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", complete_with_push)

    t = NOW
    for secs in (300, 600, 1200, 2400, 3600, 3600):   # levels 0..5, plateau at the 3600 cap
        result = await dispatcher.process_next_due(t)
        assert result.action == "ran"
        tk = store.get_ticket(tid)
        assert tk.status == "deferred"
        assert tk.not_before == (t + timedelta(seconds=secs)).isoformat()
        t = t + timedelta(seconds=secs)   # advance to the next due time


async def test_claim_clears_schedule_notice_when_one_was_pending(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    cleared = _stub_clear_schedule(monkeypatch)
    tid = _enqueue(pr=70)
    _set_comment_id(db_exec, tid, 7070)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
    )

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=7070)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert cleared == [(70, 7070)]
    assert store.get_ticket(tid).notice_not_before is None


async def test_claim_time_clear_schedule_notice_persists_a_recreated_comment_id(
    monkeypatch, db_exec
):
    """clear_schedule_notice's own id must be persisted even when the ticket's
    review attempt fails right after -- this call site has no other route to
    ever record a recreated id."""
    cleared = []

    def fake_clear(repo, pr, comment_id=None):
        cleared.append((pr, comment_id))
        return SimpleNamespace(id=6060)

    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice", fake_clear)
    tid = _enqueue(pr=73)
    _set_comment_id(db_exec, tid, 7373)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
    )

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    await dispatcher.process_next_due(NOW)
    assert cleared == [(73, 7373)]
    assert store.get_ticket(tid).comment_id == 6060


async def test_claim_time_clear_schedule_notice_confirmed_loss_clears_visible_review(
    monkeypatch, db_exec
):
    """clear_schedule_notice returns None -- not a recreated comment -- when
    the bot's comment is confirmed gone (unlike append_schedule_notice/
    append_review_footnote, it never creates a fallback). That must be
    treated as the same content-loss signal handled at every other
    comment-touching call site (post_pending_notices, the terminal-failure
    branch), not silently ignored -- otherwise a later gate in the same or a
    future process_next_due call still trusts the stale last_reviewed_at and
    can suppress a placeholder, leaving the PR with zero bot comments."""
    monkeypatch.setattr(
        dispatcher.github_app, "clear_schedule_notice",
        lambda repo, pr, comment_id=None: None,
    )
    tid = _enqueue(pr=75)
    _set_comment_id(db_exec, tid, 7575)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
    )

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    await dispatcher.process_next_due(NOW)

    assert store.get_ticket(tid).last_reviewed_at is None


async def test_claim_does_not_call_clear_when_no_notice_pending(monkeypatch):
    _stub_comments(monkeypatch)
    cleared = _stub_clear_schedule(monkeypatch)
    _enqueue(pr=71)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert cleared == []


async def test_claim_clear_failure_does_not_block_review_attempt(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=72)
    _set_comment_id(db_exec, tid, 7272)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
    )

    def boom_clear(repo, pr, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice", boom_clear)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=7272)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"                       # review still proceeded
    assert store.get_ticket(tid).notice_not_before == "2026-01-01T11:00:00+00:00"


def _stub_append_schedule(monkeypatch):
    posted = []

    def fake_append(repo, pr, footnote, comment_id=None):
        posted.append((pr, footnote, comment_id))
        return SimpleNamespace(id=comment_id)

    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice", fake_append)
    return posted


async def test_post_pending_notices_posts_for_matching_ticket(monkeypatch, db_exec):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=80)
    _set_comment_id(db_exec, tid, 8080)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert posted and posted[0][0] == 80
    assert posted[0][2] == 8080
    assert "13:00 UTC" in posted[0][1]
    assert store.get_ticket(tid).notice_not_before == future.isoformat()
    assert store.get_ticket(tid).comment_id == 8080  # unchanged id re-persisted


async def test_post_pending_notices_confirmed_loss_clears_visible_review(monkeypatch, db_exec):
    """The bot's comment was deleted: append_schedule_notice has to create a
    fresh one with a different id. That's confirmed content loss -- persist
    the new id AND clear last_reviewed_at."""
    posted = []

    def fake_append(repo, pr, footnote, comment_id=None):
        posted.append((pr, footnote, comment_id))
        return SimpleNamespace(id=9999)

    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice", fake_append)
    tid = _enqueue(pr=84)
    _set_comment_id(db_exec, tid, 8484)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert posted[0][2] == 8484                  # old id threaded into the call
    t = store.get_ticket(tid)
    assert t.comment_id == 9999                  # new id from GitHub persisted
    assert t.last_reviewed_at is None            # visible-review flag honestly cleared


async def test_post_pending_notices_does_not_repost_when_marker_matches(monkeypatch, db_exec):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=81)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), future.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 0
    assert posted == []


async def test_post_pending_notices_per_ticket_failure_does_not_block_others(monkeypatch, db_exec):
    tid1 = _enqueue(pr=82)
    tid2 = _enqueue(pr=83)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid1),
    )
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid2),
    )

    calls = []

    def flaky_append(repo, pr, footnote, comment_id=None):
        calls.append(pr)
        if pr == 82:
            raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice", flaky_append)

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1                                       # only the successful one counted
    assert set(calls) == {82, 83}                            # both attempted
    assert store.get_ticket(tid1).notice_not_before is None  # failed post -> marker not set
    assert store.get_ticket(tid2).notice_not_before == future.isoformat()


async def test_notice_sweep_uses_usage_cap_wording_for_a_usage_capped_ticket(
    monkeypatch, db_exec
):
    """The sweep runs on a later iteration with no memory of why the ticket
    was deferred -- the ticket row is the durable carrier (design doc §4.1)."""
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=99)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "defer_reason='usage_cap' WHERE id=%s",
        (CAP_RESET_AT.isoformat(), NOW.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert "usage limit" in posted[0][1].lower()


async def test_notice_sweep_uses_the_unchanged_wording_when_defer_reason_is_null(
    monkeypatch, db_exec
):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=100)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        ((NOW + timedelta(hours=1)).isoformat(), NOW.isoformat(), tid),
    )

    await dispatcher.post_pending_notices(NOW)

    assert "usage limit" not in posted[0][1].lower()
    assert "Re-review scheduled ~13:00 UTC" in posted[0][1]


async def test_claimed_ticket_runs_against_the_db_override(monkeypatch):
    """The behavioral guarantee: a mid-session override changes which provider
    actually runs, with no restart and no redeploy."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    store.set_provider_override("groq", NOW.isoformat())
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from providers.active import active_provider
        seen.append(active_provider())
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    assert seen == ["groq"]


async def test_claim_falls_back_to_env_when_the_override_read_fails(monkeypatch):
    """Fail-safe: an unreachable override must degrade to the configured
    provider, never abort the review, and never keep serving a stale cached
    override from a previous successful refresh."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    # A prior successful refresh cached a DIFFERENT provider. If the failure
    # handler merely logged and left the cache alone, active_provider() would
    # keep returning "groq" forever -- this is what catches that.
    active.set_override_cache("groq")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_provider_override", boom)
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from providers.active import active_provider
        seen.append(active_provider())
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    assert seen == ["gemini"]
    assert result.action == "ran"


async def test_claimed_ticket_uses_the_db_key_index_override(monkeypatch):
    """The behavioral guarantee: a mid-session key-index override changes
    which credential slot actually resolves, with no restart and no
    redeploy."""
    _stub_comments(monkeypatch)
    store.set_key_index_override("groq", 2, NOW.isoformat())
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from providers import key_index
        seen.append(key_index.active_key_index("groq"))
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    assert seen == [2]


async def test_claim_falls_back_to_index_zero_when_the_key_index_read_fails(monkeypatch):
    """Fail-safe: an unreachable override must degrade to index 0, never
    abort the review, and never keep serving a stale cached override from a
    previous successful refresh."""
    from providers import key_index

    _stub_comments(monkeypatch)
    # A prior successful refresh cached a DIFFERENT index. If the failure
    # handler merely logged and left the cache alone, active_key_index()
    # would keep returning 2 forever -- this is what catches that.
    key_index.set_override_cache({"groq": 2})

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_all_key_index_overrides", boom)
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        seen.append(key_index.active_key_index("groq"))
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    assert seen == [0]
    assert result.action == "ran"


async def test_claimed_ticket_uses_the_db_cooldown_override(monkeypatch):
    """The behavioral guarantee: a mid-session cooldown override changes the
    next scheduled re-review, with no restart and no redeploy."""
    _stub_comments(monkeypatch)
    # Restore the real store.get_cooldown_overrides -- the _env fixture
    # stubs it to read settings live (Task 6's no-fallback contract), which
    # would shadow this test's own DB write below.
    monkeypatch.setattr(dispatcher.store, "get_cooldown_overrides", _REAL_GET_COOLDOWN_OVERRIDES)
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=NOW.isoformat())

    async def fake_attempt(repo, pr, comment_id=None):
        # A push lands mid-review -> dirty flag -> the cooldown actually gets
        # consulted on the re-arm path (see store.finalize_review).
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=1, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    t = store.get_ticket(1)
    expected = NOW + timedelta(seconds=30.0)  # level 0 -> base override, not env 300s
    assert t.not_before == expected.isoformat()


async def test_claim_surfaces_a_real_failure_when_the_cooldown_read_fails(monkeypatch):
    """cooldown_config is DB-only, no env fallback (Task 6) -- an unreachable
    cooldown override degrades the cache to (None, None, None), so the
    re-arm computation (store.effective_cooldown, consulted right after a
    completed review) has nothing to compute with and must raise rather than
    silently guess a cooldown. Unlike the pre-refactor "falls back to env"
    behavior, this is a real, visible failure: it propagates out of
    process_next_due to run_forever's own broad except (the ticket stays
    'running' until the next recover_on_startup pass), the same category of
    failure as a process crash mid-review."""
    _stub_comments(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_cooldown_overrides", boom)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    with pytest.raises(TypeError):
        await dispatcher.process_next_due(NOW)


async def test_over_token_cap_defers_without_calling_attempt_review(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    tid = _enqueue(pr=90)
    _set_comment_id(db_exec, tid, 9090)
    _record_usage(db_exec, tokens=500)          # exactly at the cap -> already over

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)

    assert result.action == "deferred"
    assert called == []                          # the whole point: no call is made at all
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.defer_reason == "usage_cap"
    assert t.not_before == CAP_RESET_AT.isoformat()
    assert posted and posted[0][0] == 90
    assert "usage limit" in posted[0][1].lower()
    assert posted[0][2] == 9090                  # threaded comment_id preserved


async def test_usage_cap_placeholder_persists_a_recreated_comment_id(monkeypatch, db_exec):
    posted = []

    def fake_upsert(repo, pr, body, comment_id=None):
        posted.append((pr, body, comment_id))
        return SimpleNamespace(id=777)

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", fake_upsert)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    tid = _enqueue(pr=90)
    _set_comment_id(db_exec, tid, 9090)
    _record_usage(db_exec, tokens=500)

    await dispatcher.process_next_due(NOW)
    assert posted[0][2] == 9090
    assert store.get_ticket(tid).comment_id == 777


async def test_under_token_cap_runs_normally(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    tid = _enqueue(pr=91)
    _record_usage(db_exec, tokens=499)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_usage_before_the_bucket_start_does_not_count(monkeypatch, db_exec):
    """Yesterday's spend must not hold today's reviews hostage."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    _enqueue(pr=92)
    _record_usage(db_exec, tokens=9000, created_at="2026-01-01T03:00:00+00:00")

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_another_key_slots_usage_does_not_count(monkeypatch, db_exec):
    """A slot swap grants a fresh budget with no special-case code -- the
    query is scoped to whatever active_key_index() resolves to now."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    _enqueue(pr=93)
    _record_usage(db_exec, tokens=9000, key_index=1)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_no_cap_configured_never_queries_usage(monkeypatch, db_exec):
    """Feature off by default: an existing deployment must not even pay for
    the query, let alone change behavior."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    _enqueue(pr=96)
    _record_usage(db_exec, tokens=10**9, cost=10**6)

    queried = []
    monkeypatch.setattr(
        dispatcher.store, "get_key_usage",
        lambda *a, **kw: queried.append(a) or 0,
    )

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"
    assert queried == []


async def test_usage_check_failure_fails_open_and_runs_the_review(monkeypatch, db_exec):
    """Usage-cap enforcement degrading to off is the same posture as every
    other override here degrading to its safe default -- a broken usage query
    must never be able to block every review."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 1)
    _enqueue(pr=97)
    _record_usage(db_exec, tokens=9000)

    def boom(*args, **kwargs):
        raise RuntimeError("usage query exploded")

    monkeypatch.setattr(dispatcher.store, "get_key_usage", boom)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_effective_caps_failure_fails_open_and_runs_the_review(monkeypatch):
    """Same fail-open posture as test_usage_check_failure_fails_open_and_runs_
    the_review above, but for usage_cap_config.effective_caps() itself
    raising -- it must be inside the same try as the rest of the cap
    computation (dispatcher.py's own comment says "the whole computation ...
    sits inside one try"), not read before it."""
    _stub_comments(monkeypatch)
    _enqueue(pr=99)

    def boom():
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(dispatcher.usage_cap_config, "effective_caps", boom)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_capped_ticket_with_a_visible_review_gets_no_placeholder(monkeypatch, db_exec):
    """A good review already on the PR is preserved; the notice sweep shows
    the schedule footnote instead (same rule as every other deferral)."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 1)
    tid = _enqueue(pr=98)
    db_exec("UPDATE tickets SET last_reviewed_at=%s WHERE id=%s", (NOW.isoformat(), tid))
    _record_usage(db_exec, tokens=9000)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "deferred"
    assert posted == []


async def test_model_override_refresh_degrades_to_env_on_db_failure(monkeypatch):
    """Same fail-safe shape as the provider/cooldown/key-index refreshes: a
    failing refresh must never abort a review and never leave a stale cache."""
    from providers import active_model
    from review_queue import dispatcher, store

    active_model.set_override_cache({"groq": "stale-model"})

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_all_model_overrides", _boom)
    await dispatcher._refresh_model_overrides()
    assert active_model.active_model("groq") != "stale-model"
    active_model.reset_override_cache()


async def test_usage_cap_override_refresh_degrades_to_env_on_db_failure(monkeypatch):
    """Mirrors test_model_override_refresh_degrades_to_env_on_db_failure above,
    exact same shape, for _refresh_usage_cap_overrides (added by Task 5): a
    failing DB read must degrade the cache to "no override" rather than keep a
    stale token cap in force."""
    from review_queue import dispatcher, store, usage_cap_config

    usage_cap_config.set_override_cache(999, None)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_usage_cap_overrides", _boom)
    await dispatcher._refresh_usage_cap_overrides()
    tokens, _reset = usage_cap_config.effective_caps()
    assert tokens != 999
    usage_cap_config.reset_override_cache()


async def test_review_draft_override_refresh_degrades_to_env_on_db_failure(monkeypatch):
    """Same fail-safe shape as the other refreshes: a failing DB read must
    degrade the cache to "no override" -- which, with no env fallback
    (Task 6), reads back as None ("config not available this tick"), not a
    stale value and not a silent env default."""
    from review_queue import dispatcher, review_draft_config, store

    review_draft_config.set_override_cache(True)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_review_draft_override", _boom)
    await dispatcher._refresh_review_draft_override()
    assert review_draft_config.effective_review_draft_prs() is None
    review_draft_config.reset_override_cache()


async def test_process_next_due_refreshes_the_review_draft_override(monkeypatch):
    """The refresh must run once per claimed ticket, same cadence as the
    other overrides, so a DB-flipped value takes effect on the next ticket
    with no redeploy."""
    from review_queue import review_draft_config

    _stub_comments(monkeypatch)
    _enqueue(pr=1)
    monkeypatch.setattr(store, "get_review_draft_override", lambda: True)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    await dispatcher.process_next_due(NOW)

    assert review_draft_config.effective_review_draft_prs() is True
    review_draft_config.reset_override_cache()


async def test_under_cap_still_respects_the_blocked_provider_gate(monkeypatch, db_exec):
    """The cap check and the reactive blocked-provider gate are two
    independent gates in sequence: when the cap is configured but current
    usage is comfortably under it (not capped), process_next_due must fall
    through cleanly to the pre-existing blocked-provider gate below it,
    rather than the cap check swallowing or masking that gate."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    tid = _enqueue(pr=99)
    _set_comment_id(db_exec, tid, 9099)
    _record_usage(db_exec, tokens=1)          # far under the cap -- not capped
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)

    assert result.action == "deferred"
    assert called == []                          # never fired a doomed call
    t = store.get_ticket(tid)
    assert t.defer_reason is None                # provider gate, not the cap
    assert posted and "rate limit" in posted[0][1].lower()
    assert posted[0][2] == 9099


async def test_run_forever_refreshes_idle_sleep_from_db_not_settings(monkeypatch):
    """DISPATCHER_IDLE_SLEEP_SECONDS is DB-only (no env fallback) -- run_forever
    must sleep the DB value, not settings.dispatcher_idle_sleep_seconds, once
    a refresh has happened."""
    dispatcher._idle_sleep_seconds = None
    dispatcher._last_idle_sleep_refresh = 0.0
    monkeypatch.setattr(store, "get_idle_sleep_seconds", lambda: 7.5)
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError  # stop run_forever after one iteration

    monkeypatch.setattr(dispatcher.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(dispatcher, "process_next_due", AsyncMock(return_value=None))
    monkeypatch.setattr(dispatcher, "post_pending_notices", AsyncMock(return_value=0))
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.run_forever()
    assert sleeps == [7.5]
