"""Tests for the `reviews` table: record_review() + the dashboard read
helpers. Uses the shared Postgres test harness (tests/conftest.py)."""
from __future__ import annotations

import pytest

from review_queue import store
from specialists.schemas import ReviewResult, SpecialistResult


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def _review(pr_number=42, cost=0.0021) -> ReviewResult:
    return ReviewResult(
        pr_number=pr_number,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[{"severity": "high", "file": "app/x.py", "line": 3,
                           "description": "d", "fix": "f"}],
                elapsed_ms=10,
                tokens_in=5,
                tokens_out=2,
            ),
            SpecialistResult(
                name="Performance",
                status="failed",
                findings=[],
                error="rate limited",
                elapsed_ms=1,
            ),
        ],
        total_elapsed_ms=11,
        total_tokens_in=5,
        total_tokens_out=2,
        est_cost_usd=cost,
    )


def test_record_review_round_trips_through_dashboard_reviews():
    store.record_review(
        "owner/repo", 42, _review(), comment_id=999, now="2026-08-11T12:00:00+00:00",
        key_index=0,
    )

    rows = store.dashboard_reviews()
    assert len(rows) == 1
    row = rows[0]
    assert row["repo"] == "owner/repo"
    assert row["pr_number"] == 42
    assert row["provider"] == "groq"
    assert row["model"] == "llama-3.3-70b-versatile"
    assert row["created_at"] == "2026-08-11T12:00:00+00:00"
    assert row["elapsed_ms"] == 11
    assert row["tokens_in"] == 5
    assert row["tokens_out"] == 2
    assert row["est_cost_usd"] == 0.0021
    assert row["comment_url"] == "https://github.com/owner/repo/pull/42#issuecomment-999"
    assert row["specialists"][0]["name"] == "Security"
    assert row["specialists"][0]["findings"][0]["severity"] == "high"
    assert row["specialists"][1]["status"] == "failed"
    assert row["specialists"][1]["error"] == "rate limited"


def test_record_review_with_no_comment_id_has_no_comment_url():
    store.record_review(
        "owner/repo", 43, _review(pr_number=43), comment_id=None,
        now="2026-08-11T12:00:00+00:00", key_index=0,
    )
    assert store.dashboard_reviews()[0]["comment_url"] is None


def test_dashboard_reviews_orders_newest_first_and_respects_limit():
    store.record_review(
        "owner/repo", 1, _review(pr_number=1), 1, now="2026-08-11T12:00:00+00:00", key_index=0
    )
    store.record_review(
        "owner/repo", 2, _review(pr_number=2), 2, now="2026-08-11T12:00:01+00:00", key_index=0
    )
    store.record_review(
        "owner/repo", 3, _review(pr_number=3), 3, now="2026-08-11T12:00:02+00:00", key_index=0
    )

    rows = store.dashboard_reviews(limit=2)
    assert [r["pr_number"] for r in rows] == [3, 2]


def test_dashboard_stats_aggregates_across_all_reviews():
    store.record_review(
        "owner/repo", 1, _review(cost=0.001), 1, now="2026-08-11T12:00:00+00:00", key_index=0
    )
    store.record_review(
        "owner/repo", 2, _review(cost=0.002), 2, now="2026-08-11T12:00:01+00:00", key_index=0
    )

    stats = store.dashboard_stats()
    assert stats["total_reviews"] == 2
    assert stats["total_cost_usd"] == pytest.approx(0.003)
    assert stats["avg_elapsed_ms"] == 11


def test_dashboard_stats_on_empty_table():
    assert store.dashboard_stats() == {
        "total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0,
    }


def test_dashboard_queue_counts_includes_all_statuses_defaulted_to_zero():
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha1", provider="groq",
        now="2026-08-11T12:00:00+00:00",
    )
    counts = store.dashboard_queue_counts()
    assert counts == {
        "pending": 1, "running": 0, "deferred": 0, "retrying": 0, "done": 0, "failed": 0,
        "cancelled": 0,
    }


def test_dashboard_failed_tickets_orders_newest_first_and_respects_limit():
    for pr, ts in ((1, "2026-08-11T12:00:00+00:00"), (2, "2026-08-11T12:00:01+00:00")):
        ticket_id = store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=pr, head_sha="sha", provider="groq", now=ts,
        )
        store.claim_next_due(now=ts)
        store.mark_failed(ticket_id, now=ts, error=f"boom {pr}")

    rows = store.dashboard_failed_tickets(limit=1)
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 2
    assert rows[0]["repo"] == "owner/repo"
    assert rows[0]["provider"] == "groq"
    assert rows[0]["last_error"] == "boom 2"


def test_dashboard_failed_tickets_excludes_non_failed_statuses():
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha", provider="groq",
        now="2026-08-11T12:00:00+00:00",
    )
    assert store.dashboard_failed_tickets() == []


def test_record_review_persists_the_key_index_it_was_given(db_query):
    """Which key slot paid for a review is the whole basis of the per-slot
    usage cap (design doc §3) -- it must be persisted, not inferred later
    from whatever slot happens to be active at read time."""
    store.record_review(
        "owner/repo", 44, _review(pr_number=44), comment_id=None,
        now="2026-08-11T12:00:00+00:00", key_index=2,
    )
    rows = db_query("SELECT key_index FROM reviews WHERE pr_number = 44")
    assert rows == [(2,)]
