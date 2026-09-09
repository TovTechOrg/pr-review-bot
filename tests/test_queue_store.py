"""Ticket store: enqueue/collapse, atomic claim, defer, recover-on-startup.

Uses the shared Postgres test harness (``db``/``db_exec``/``db_query`` from
tests/conftest.py) — a real Postgres, truncated between tests.
"""
from __future__ import annotations

import pytest

from config import settings
from review_queue import cooldown_config, store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"
FUTURE = "2026-01-01T18:00:00+00:00"
PAST = "2026-01-01T06:00:00+00:00"
T_COOL = "2026-01-01T12:05:00+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


@pytest.fixture(autouse=True)
def _clean_cooldown_cache():
    cooldown_config.reset_override_cache()
    yield
    cooldown_config.reset_override_cache()


def _enqueue(repo="owner/repo", pr=1, sha="sha1", provider="groq", now=T0):
    return store.enqueue_or_update(
        repo_full_name=repo, pr_number=pr, head_sha=sha, provider=provider, now=now
    )


def test_enqueue_creates_pending_ticket():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.repo_full_name == "owner/repo"
    assert t.pr_number == 1
    assert t.head_sha == "sha1"
    assert t.provider == "groq"
    assert t.not_before is None


def test_enqueue_same_pr_collapses_and_updates_head_sha():
    tid1 = _enqueue(sha="sha1")
    tid2 = _enqueue(sha="sha2", now=T1)
    assert tid1 == tid2  # one row per (repo, pr)
    assert store.get_ticket(tid1).head_sha == "sha2"


def test_claim_next_due_returns_pending_and_marks_running():
    tid = _enqueue()
    claimed = store.claim_next_due(now=T1)
    assert claimed.id == tid
    assert store.get_ticket(tid).status == "running"


def test_claim_next_due_returns_none_when_empty():
    assert store.claim_next_due(now=T1) is None


def test_claim_is_fifo_by_enqueued_at():
    a = _enqueue(pr=1, now=T0)
    _enqueue(pr=2, now=T1)
    assert store.claim_next_due(now=T1).id == a


def test_deferred_ticket_not_claimed_before_not_before():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    assert store.claim_next_due(now=T1) is None            # not yet due
    assert store.claim_next_due(now=FUTURE).id == tid       # due now


def test_recover_on_startup_resets_running_to_pending():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.recover_on_startup(now=T1)
    assert store.get_ticket(tid).status == "pending"


def test_cancel_ticket_cancels_a_pending_ticket():
    tid = _enqueue()
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    t = store.get_ticket(tid)
    assert t.status == "cancelled"
    assert t.updated_at == T1


def test_cancel_ticket_returns_the_cancelled_ticket():
    """The return value is how webhook.py's cancel handler learns whether to
    strip a live schedule-notice footnote from GitHub -- a cancelled ticket
    is never claimed again, so it's the caller's only chance."""
    tid = _enqueue()
    cancelled = store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    assert cancelled is not None
    assert cancelled.id == tid
    assert cancelled.status == "cancelled"


def test_cancel_ticket_returns_none_when_no_ticket_matches():
    assert store.cancel_ticket(repo_full_name="owner/repo", pr_number=999, now=T1) is None


def test_cancel_ticket_cancels_a_deferred_ticket():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    assert store.get_ticket(tid).status == "cancelled"


def test_cancel_ticket_cancels_a_retrying_ticket():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=FUTURE, now=T0)
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    assert store.get_ticket(tid).status == "cancelled"


def test_cancel_ticket_does_not_touch_a_running_ticket():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    assert store.get_ticket(tid).status == "running"


def test_cancel_ticket_is_a_noop_when_no_ticket_exists():
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=999, now=T1)  # must not raise


def test_cancel_ticket_does_not_touch_an_already_terminal_ticket():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T0, error="boom")
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    assert store.get_ticket(tid).status == "failed"          # untouched, not re-labeled


def test_cancelled_ticket_is_not_claimable():
    tid = _enqueue()
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    assert store.claim_next_due(now=T1) is None
    assert store.get_ticket(tid).status == "cancelled"


def test_push_to_cancelled_ticket_revives_to_pending_when_never_reviewed():
    tid = _enqueue()
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.head_sha == "sha2"


def test_push_to_cancelled_ticket_respects_cooldown_when_previously_reviewed():
    cooldown_config.set_override_cache(3600.0, 7200.0, 2.0)
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T0, rereview_cooldown_level=0)
    store.cancel_ticket(repo_full_name="owner/repo", pr_number=1, now=T1)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    assert store.get_ticket(tid).status == "deferred"        # still cooling down


def test_discard_skipped_ticket_deletes_a_clean_ticket():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.discard_skipped_ticket(tid, now=T1)
    assert store.get_ticket(tid) is None


def test_discard_skipped_ticket_rearms_pending_when_a_push_landed_mid_flight():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)          # -> running
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )  # sets rereview_requested=1 without touching status
    store.discard_skipped_ticket(tid, now=T1)
    t = store.get_ticket(tid)
    assert t is not None
    assert t.status == "pending"
    assert t.rereview_requested == 0
    assert t.head_sha == "sha2"           # the newer push's sha is preserved


def test_discard_skipped_ticket_is_a_noop_when_no_ticket_exists():
    store.discard_skipped_ticket(999999, now=T1)  # must not raise


def test_discard_skipped_ticket_preserves_a_previously_reviewed_tickets_state(db_exec):
    """A ticket that was already reviewed once must not lose its cooldown
    escalation / comment tracking just because a LATER push happens to be a
    draft or an empty diff -- otherwise a dummy empty-diff/draft push could
    be used to reset the anti-churn cooldown deliberately."""
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0)
    store.set_comment_id(tid, 4242)
    db_exec("UPDATE tickets SET cooldown_level = 3, status = 'running' WHERE id = %s", (tid,))

    store.discard_skipped_ticket(tid, now=T1)

    t = store.get_ticket(tid)
    assert t is not None
    assert t.status == "done"
    assert t.last_reviewed_at == T0
    assert t.comment_id == 4242
    assert t.cooldown_level == 3
    assert t.not_before is None
    assert t.rereview_requested == 0


def test_discard_skipped_ticket_still_rearms_pending_over_preserving_done_state(db_exec):
    """A concurrent push mid-flight must win over preserving the prior
    review's 'done' state -- that push might carry real content and must
    never be lost."""
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0)
    db_exec(
        "UPDATE tickets SET status = 'running', rereview_requested = 1, head_sha = 'sha3' "
        "WHERE id = %s",
        (tid,),
    )

    store.discard_skipped_ticket(tid, now=T1)

    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.rereview_requested == 0
    assert t.head_sha == "sha3"


def test_migrate_repo_rename_moves_a_ticket_to_the_new_name():
    tid = _enqueue(repo="owner/old-name", pr=1)
    store.migrate_repo_rename("owner/old-name", "owner/new-name", now=T1)
    t = store.get_ticket(tid)
    assert t.repo_full_name == "owner/new-name"
    assert t.updated_at == T1


def test_migrate_repo_rename_moves_reviews_history_too(db_exec, db_query):
    db_exec(
        "INSERT INTO reviews (repo_full_name, pr_number, provider, model, comment_id, "
        "created_at, total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, "
        "results, key_index) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb,%s)",
        ("owner/old-name", 1, "groq", "m", None, T0, 1, 0, 0, 0.0, 0),
    )
    store.migrate_repo_rename("owner/old-name", "owner/new-name", now=T1)
    assert db_query("SELECT repo_full_name FROM reviews WHERE pr_number = 1") == [
        ("owner/new-name",)
    ]


def test_migrate_repo_rename_cancels_a_colliding_ticket_instead_of_erroring():
    """A fresh webhook under the new name may already have created a ticket
    for the same PR before the rename is detected -- the stale old-named row
    must be cancelled, not migrated into a unique-constraint violation."""
    stale_tid = _enqueue(repo="owner/old-name", pr=5)
    fresh_tid = _enqueue(repo="owner/new-name", pr=5)

    store.migrate_repo_rename("owner/old-name", "owner/new-name", now=T1)

    stale = store.get_ticket(stale_tid)
    assert stale.status == "cancelled"
    assert stale.repo_full_name == "owner/old-name"   # left in place, not moved
    fresh = store.get_ticket(fresh_tid)
    assert fresh.repo_full_name == "owner/new-name"
    assert fresh.status == "pending"                  # untouched


def test_migrate_repo_rename_is_a_noop_when_nothing_matches():
    store.migrate_repo_rename("owner/nonexistent", "owner/new-name", now=T1)  # must not raise


def test_defer_rate_limited_does_not_increment_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 0


def test_defer_failed_increments_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1


def test_retrying_ticket_is_claimable_once_not_before_passes():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=T1, now=T0)
    claimed = store.claim_next_due(now=T1)
    assert claimed.id == tid
    assert store.get_ticket(tid).status == "running"


def test_mark_failed_sets_status_failed():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T1, error="boom")
    t = store.get_ticket(tid)
    assert t.status == "failed"
    assert t.updated_at == T1


def test_mark_failed_persists_the_error_message():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T1, error="boom: connection reset")
    t = store.get_ticket(tid)
    assert t.last_error == "boom: connection reset"


def test_push_during_deferred_rides_out_keeping_not_before():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)   # provider wait
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"       # not reset to pending
    assert t.not_before == FUTURE       # provider clock NOT shortened
    assert t.head_sha == "sha2"         # latest commit recorded


def test_push_during_retrying_rides_out_keeping_not_before_and_attempts():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.defer_failed(tid, not_before=FUTURE, now=T0)  # hard-failure backoff, attempts -> 1
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "retrying"       # not reset to pending
    assert t.not_before == FUTURE       # failure backoff deadline NOT shortened/reset
    assert t.attempts == 1              # not reset by the push
    assert t.head_sha == "sha2"         # latest commit recorded


def test_push_during_running_sets_rereview_flag_and_keeps_running():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "running"
    assert t.rereview_requested == 1
    assert t.head_sha == "sha2"


def test_push_to_done_ticket_within_cooldown_re_arms_deferred():
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.finalize_review(
        tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0
    )  # done, last_reviewed_at=T0
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL       # last_reviewed_at(T0) + 300s
    assert t.attempts == 0


def test_push_to_done_ticket_past_cooldown_re_arms_pending():
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=FUTURE
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.not_before is None


def test_recover_on_startup_clears_rereview_flag(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)                       # -> running
    db_exec("UPDATE tickets SET rereview_requested = 1 WHERE id = %s", (tid,))
    store.recover_on_startup(now=T1)
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.rereview_requested == 0


def test_new_ticket_has_rereview_and_last_reviewed_defaults():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.rereview_requested == 0
    assert t.last_reviewed_at is None


def test_finalize_review_without_flag_marks_done():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=7, comment_id=99
    )
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.comment_id == 99
    assert t.last_reviewed_at == T1
    assert t.not_before is None
    # non-dirty -> level unchanged (passed value ignored)
    assert store.get_ticket(tid).cooldown_level == 0


def test_finalize_review_none_comment_id_does_not_erase_persisted_id():
    """A future completion path that forgets to pass a real comment_id must not
    silently demote the ticket back to scan-only identity (losing the id)."""
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=0, comment_id=555
    )
    assert store.get_ticket(tid).comment_id == 555

    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=0, comment_id=None
    )
    assert store.get_ticket(tid).comment_id == 555   # NOT overwritten to None


def test_set_comment_id_persists_the_id():
    tid = _enqueue()
    store.set_comment_id(tid, 777)
    assert store.get_ticket(tid).comment_id == 777


def test_set_comment_id_none_is_a_noop():
    tid = _enqueue()
    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=0, comment_id=555
    )
    store.set_comment_id(tid, None)
    assert store.get_ticket(tid).comment_id == 555


def test_clear_visible_review_nulls_last_reviewed_at():
    tid = _enqueue()
    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=0, comment_id=555
    )
    assert store.get_ticket(tid).last_reviewed_at is not None

    store.clear_visible_review(tid)
    t = store.get_ticket(tid)
    assert t.last_reviewed_at is None
    assert t.comment_id == 555  # unrelated column untouched


def test_finalize_review_with_flag_re_arms_deferred_at_cooldown_and_resets_attempts(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_failed(tid, not_before=T0, now=T0)   # attempts -> 1
    store.claim_next_due(now=T0)          # -> running again
    # Simulate a push during the run setting the dirty flag:
    db_exec("UPDATE tickets SET rereview_requested = 1 WHERE id = %s", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=2)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.attempts == 0
    assert t.rereview_requested == 0
    assert t.last_reviewed_at == T1
    assert t.cooldown_level == 2  # dirty -> stores passed level


def test_finalize_review_dirty_flag_stores_passed_cooldown_level(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)
    db_exec("UPDATE tickets SET rereview_requested = 1 WHERE id = %s", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=3)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.cooldown_level == 3


def test_enqueue_or_update_serializes_under_concurrent_writers():
    """Two threads enqueue the same PR concurrently. With SELECT ... FOR UPDATE
    they serialize (row lock taken up front) rather than interleave: both
    complete without error and the final row is consistent. Serialization
    smoke test — a true race needs threads and is timing-dependent, so this
    asserts the observable invariant (no lost/corrupt write), not a specific
    interleaving."""
    import threading

    tid = _enqueue(pr=50, sha="sha0")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(sha: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.enqueue_or_update(
                repo_full_name="owner/repo", pr_number=50,
                head_sha=sha, provider="groq", now=T1,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in ("shaA", "shaB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []                          # no lock error / no crash
    final = store.get_ticket(tid)
    assert final.head_sha in ("shaA", "shaB")    # a consistent, complete write won
    assert final.status == "pending"


T_400 = "2026-01-01T12:06:40+00:00"   # T0 (12:00:00) + 400s


def _make_done(db_exec, tid, last_reviewed_at, level, now=T0):
    """Directly force a ticket to a completed state (bypasses finalize_review so
    Task 3 tests don't depend on finalize's Task-4 signature)."""
    db_exec(
        "UPDATE tickets SET status='done', last_reviewed_at=%s, cooldown_level=%s, "
        "updated_at=%s WHERE id=%s",
        (last_reviewed_at, level, now, tid),
    )


def test_due_after_cooldown_branches():
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    # never reviewed -> pending, level 0
    assert store._due_after_cooldown(None, T1, 0) == ("pending", None, 0)
    # within window (level 0 -> 300s; T0=12:00:00, T1=12:00:01) -> deferred, escalate to 1
    assert store._due_after_cooldown(T0, T1, 0) == ("deferred", T_COOL, 1)
    # elapsed -> pending, reset to 0
    assert store._due_after_cooldown(T0, FUTURE, 0) == ("pending", None, 0)
    # at level 1 the window is 600s; a push 400s after T0 is still within -> deferred, escalate to 2
    status, nb, lvl = store._due_after_cooldown(T0, T_400, 1)
    assert status == "deferred"
    assert nb == "2026-01-01T12:10:00+00:00"   # T0 + 600s
    assert lvl == 2


def test_enqueue_push_within_cooldown_escalates_level(db_exec):
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    tid = _enqueue(sha="sha1")
    # last review T0, already at level 1 (eff=600s)
    _make_done(db_exec, tid, last_reviewed_at=T0, level=1)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T_400
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == "2026-01-01T12:10:00+00:00"   # T0 + 600s
    assert t.cooldown_level == 2                          # escalated
    assert t.head_sha == "sha2"


def test_enqueue_push_after_cooldown_resets_level(db_exec):
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    tid = _enqueue(sha="sha1")
    _make_done(db_exec, tid, last_reviewed_at=T0, level=3)   # window eff(3)=2400s (until 12:40)
    store.enqueue_or_update(   # FUTURE = 18:00, well past the window -> quiet -> reset
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=FUTURE
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.not_before is None
    assert t.cooldown_level == 0


def test_effective_cooldown_escalates_and_caps():
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    assert store.effective_cooldown(0) == 300.0     # level 0 == today's flat cooldown
    assert store.effective_cooldown(1) == 600.0
    assert store.effective_cooldown(2) == 1200.0
    assert store.effective_cooldown(3) == 2400.0
    assert store.effective_cooldown(4) == 3600.0    # 300*16=4800 -> capped
    assert store.effective_cooldown(50) == 3600.0   # capped, no 2**50 blowup


def test_next_cooldown_level_increments_and_guards():
    assert store.next_cooldown_level(0) == 1
    assert store.next_cooldown_level(4) == 5
    assert store.next_cooldown_level(30) == 30      # _MAX_COOLDOWN_LEVEL guard


def test_effective_cooldown_uses_a_configured_factor():
    cooldown_config.set_override_cache(30.0, 300.0, 3.0)
    assert store.effective_cooldown(0) == 30.0
    assert store.effective_cooldown(1) == 90.0
    assert store.effective_cooldown(2) == 270.0
    assert store.effective_cooldown(3) == 300.0  # 810 -> capped


def test_init_pool_seeds_runtime_config_defaults_on_a_fresh_table(db_query):
    """The `db` fixture already truncated runtime_config after its own
    init_pool() call -- re-calling it here simulates a genuinely fresh
    database (a first boot against a brand-new project), which is the case
    this seeding exists for."""
    store.init_pool()
    row = db_query(
        "SELECT cooldown_base_seconds, cooldown_max_seconds, cooldown_factor, "
        "key_usage_token_cap, key_usage_reset_time_utc, review_draft_prs, "
        "llm_request_timeout_seconds, dispatcher_default_retry_after_seconds, "
        "dispatcher_failure_base_backoff_seconds, dispatcher_failure_max_backoff_seconds, "
        "dispatcher_max_failure_attempts, dispatcher_max_notice_post_attempts, "
        "dispatcher_min_retry_after_seconds, dispatcher_backoff_jitter_seconds, "
        "dispatcher_notice_sweep_batch_size, dispatcher_idle_sleep_seconds "
        "FROM runtime_config WHERE id = 1"
    )
    assert row == [
        (
            settings.dispatcher_rereview_cooldown_seconds,
            settings.dispatcher_rereview_cooldown_max_seconds,
            settings.dispatcher_rereview_cooldown_factor,
            settings.key_usage_token_cap,
            settings.key_usage_reset_time_utc.isoformat(),
            settings.review_draft_prs,
            settings.llm_request_timeout_seconds,
            settings.dispatcher_default_retry_after_seconds,
            settings.dispatcher_failure_base_backoff_seconds,
            settings.dispatcher_failure_max_backoff_seconds,
            settings.dispatcher_max_failure_attempts,
            settings.dispatcher_max_notice_post_attempts,
            settings.dispatcher_min_retry_after_seconds,
            settings.dispatcher_backoff_jitter_seconds,
            settings.dispatcher_notice_sweep_batch_size,
            settings.dispatcher_idle_sleep_seconds,
        )
    ]


def test_init_pool_does_not_overwrite_an_existing_runtime_config_row(db_query):
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    store.init_pool()
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_cooldown_overrides_default_to_none():
    assert store.get_cooldown_overrides() == (None, None, None)


def test_set_then_get_cooldown_overrides():
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)


def test_setting_cooldown_override_twice_replaces_rather_than_inserting(db_query):
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    store.set_cooldown_override(base=60.0, cap=1200.0, factor=2.0, now=T1)
    assert store.get_cooldown_overrides() == (60.0, 1200.0, 2.0)
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_cooldown_override_restores_none():
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    store.set_cooldown_override(base=None, cap=None, factor=None, now=T1)
    assert store.get_cooldown_overrides() == (None, None, None)


def test_cooldown_override_and_provider_override_coexist():
    """Both overrides live on the same singleton row -- setting one must not
    clobber the other."""
    store.set_provider_override("groq", T0)
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T1)
    assert store.get_provider_override() == "groq"
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)


def test_review_draft_override_defaults_to_none():
    assert store.get_review_draft_override() is None


def test_set_then_get_review_draft_override():
    store.set_review_draft_override(True, now=T0)
    assert store.get_review_draft_override() is True


def test_setting_review_draft_override_twice_replaces_rather_than_inserting(db_query):
    store.set_review_draft_override(True, now=T0)
    store.set_review_draft_override(False, now=T1)
    assert store.get_review_draft_override() is False
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_review_draft_override_restores_none():
    store.set_review_draft_override(True, now=T0)
    store.set_review_draft_override(None, now=T1)
    assert store.get_review_draft_override() is None


def test_review_draft_override_and_provider_override_coexist():
    store.set_provider_override("groq", T0)
    store.set_review_draft_override(True, now=T1)
    assert store.get_provider_override() == "groq"
    assert store.get_review_draft_override() is True


def test_new_ticket_has_cooldown_level_zero():
    tid = _enqueue()
    assert store.get_ticket(tid).cooldown_level == 0


def test_finalize_non_dirty_leaves_nonzero_cooldown_level(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)
    db_exec(
        "UPDATE tickets SET cooldown_level = 3 WHERE id = %s", (tid,)
    )  # rereview_requested stays 0
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=9)
    t = store.get_ticket(tid)
    assert t.status == "done"
    # non-dirty -> ELSE keeps the existing level, ignores the passed 9
    assert t.cooldown_level == 3


def test_new_ticket_has_notice_not_before_none():
    tid = _enqueue()
    assert store.get_ticket(tid).notice_not_before is None


def _seed_deferred_with_review(
    db_exec, tid, not_before, notice_not_before=None, last_reviewed_at=T0
):
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, notice_not_before=%s, "
        "last_reviewed_at=%s WHERE id=%s",
        (not_before, notice_not_before, last_reviewed_at, tid),
    )


def test_tickets_needing_notice_matches_never_notified(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
    result = store.tickets_needing_notice(now=T0, limit=100)
    assert [t.id for t in result] == [tid]


def test_tickets_needing_notice_matches_stale_marker(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=T_COOL)
    result = store.tickets_needing_notice(now=T0, limit=100)
    assert [t.id for t in result] == [tid]


def test_tickets_needing_notice_excludes_up_to_date_marker(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=FUTURE)
    assert store.tickets_needing_notice(now=T0, limit=100) == []


def test_tickets_needing_notice_excludes_no_visible_review(db_exec):
    tid = _enqueue()
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, notice_not_before=NULL "
        "WHERE id=%s",
        (FUTURE, tid),
    )
    assert store.tickets_needing_notice(now=T0, limit=100) == []


def test_tickets_needing_notice_excludes_already_due_ticket(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=PAST, notice_not_before=None)
    assert store.tickets_needing_notice(now=T0, limit=100) == []


def test_tickets_needing_notice_excludes_retrying_status(db_exec):
    tid = _enqueue()
    db_exec(
        "UPDATE tickets SET status='retrying', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=NULL WHERE id=%s",
        (FUTURE, T0, tid),
    )
    assert store.tickets_needing_notice(now=T0, limit=100) == []


def test_mark_notice_posted_persists_marker(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
    store.mark_notice_posted(tid, FUTURE)
    assert store.get_ticket(tid).notice_not_before == FUTURE
    assert store.tickets_needing_notice(now=T0, limit=100) == []


def test_clear_notice_resets_marker_to_none(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=FUTURE)
    store.clear_notice(tid)
    assert store.get_ticket(tid).notice_not_before is None


def test_tickets_needing_notice_respects_the_passed_limit(db_exec):
    tids = []
    for pr in range(1, 4):  # 3 tickets, limit is 2
        tid = _enqueue(pr=pr, now=T0)
        _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
        tids.append(tid)

    first_batch = store.tickets_needing_notice(now=T0, limit=2)
    assert len(first_batch) == 2
    assert [t.id for t in first_batch] == tids[:2]  # oldest-enqueued first

    for t in first_batch:
        store.mark_notice_posted(t.id, FUTURE)

    second_batch = store.tickets_needing_notice(now=T0, limit=2)
    assert [t.id for t in second_batch] == tids[2:]  # the leftover ticket, picked up next "tick"


def test_tickets_needing_notice_is_never_unbounded_on_a_null_column(db_exec):
    """Regression test: LIMIT (SELECT ... FROM runtime_config) used to
    silently become "no limit" whenever that column was NULL (a missing row,
    or a legacy DB before a backfill ran). The fix makes the limit a Python
    parameter with no SQL fallback, so passing a small limit here must still
    cap the result even though the DB column itself was never touched."""
    tids = []
    for pr in range(1, 4):
        tid = _enqueue(pr=pr, now=T0)
        _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
        tids.append(tid)
    assert len(store.tickets_needing_notice(now=T0, limit=1)) == 1


def test_schema_columns_match_the_ticket_dataclass(db_query):
    """_row_to_ticket does Ticket(**row), so _SCHEMA and the dataclass must agree
    exactly -- drift is a production TypeError. This also makes the hosted run's
    column check (plan Task 9) a machine-checked invariant rather than a number
    written down in a doc."""
    rows = db_query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tickets'"
    )
    assert {row[0] for row in rows} == set(store.Ticket.__dataclass_fields__)


def test_defer_usage_capped_defers_with_the_usage_cap_reason():
    """The bot's own self-imposed cap must be distinguishable from a provider
    rate-limit wait, because the PR notice wording differs (design doc §4.1).
    The ticket row is the durable carrier of that distinction -- the notice
    sweep has no other memory of why a ticket is deferred."""
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == FUTURE
    assert t.defer_reason == "usage_cap"
    assert t.attempts == 0            # a self-imposed wait is not a failure


def test_defer_rate_limited_clears_a_stale_usage_cap_reason():
    """A stale 'usage_cap' must never survive into a later, unrelated
    deferral of the same row -- it would mislabel a genuine provider wait."""
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    store.defer_rate_limited(tid, not_before=FUTURE, now=T1)
    assert store.get_ticket(tid).defer_reason is None


def test_cooldown_re_arm_on_push_clears_a_stale_usage_cap_reason(db_exec):
    cooldown_config.set_override_cache(300.0, 3600.0, 2.0)
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    # Terminal state + a recent completed review -> enqueue_or_update's
    # done/failed re-arm branch, which re-defers for the cooldown.
    db_exec(
        "UPDATE tickets SET status='done', last_reviewed_at=%s WHERE id=%s", (T0, tid)
    )
    _enqueue(now=T1)
    t = store.get_ticket(tid)
    assert t.status == "deferred"      # still inside the 300s cooldown
    assert t.defer_reason is None


def test_finalize_review_clears_a_stale_usage_cap_reason(db_exec):
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    db_exec("UPDATE tickets SET status='running', rereview_requested=1 WHERE id=%s", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=FUTURE, rereview_cooldown_level=1)
    t = store.get_ticket(tid)
    assert t.status == "deferred"      # dirty-flag re-arm
    assert t.defer_reason is None


def test_new_ticket_has_no_defer_reason():
    assert store.get_ticket(_enqueue()).defer_reason is None


def test_flat_model_override_functions_no_longer_exist():
    """The flat per-provider model override (runtime_config.{provider}_model)
    was retired in favor of slot_config -- see docs/superpowers/specs/
    2026-09-08-slotted-config-and-db-delegation-design.md section 4b. This
    pins the retirement so a future re-introduction (e.g. a copy-pasted
    partial revert) is caught immediately rather than silently resurrecting
    a write path with no reader."""
    assert not hasattr(store, "set_model_override")
    assert not hasattr(store, "get_model_override")
    assert not hasattr(store, "get_all_model_overrides")


def test_usage_cap_overrides_round_trip():
    store.set_usage_cap_override(20000, "04:30", "2026-08-15T00:00:00+00:00")
    assert store.get_usage_cap_overrides() == (20000, "04:30")


def test_usage_cap_overrides_default_to_all_none():
    assert store.get_usage_cap_overrides() == (None, None)


def test_usage_cap_overrides_write_exactly_what_they_are_given():
    """Like set_cooldown_override, this writes both fields every time; a
    caller wanting to change one is responsible for read-modify-write."""
    store.set_usage_cap_override(20000, "04:30", "2026-08-15T00:00:00+00:00")
    store.set_usage_cap_override(None, "05:00", "2026-08-15T00:01:00+00:00")
    assert store.get_usage_cap_overrides() == (None, "05:00")
