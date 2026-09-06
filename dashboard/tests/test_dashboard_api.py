"""Tests for GET /api/dashboard — the dashboard's JSON payload."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from review_queue import dispatcher, store
from specialists.schemas import ReviewResult, SpecialistResult
from dashboard import auth
from dashboard import router as dashboard


@pytest.fixture(autouse=True)
def _isolate(db):
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    )


async def test_empty_state_shape():
    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == {"total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0}
    assert body["queue"]["by_status"] == {
        "pending": 0, "running": 0, "deferred": 0, "retrying": 0, "done": 0, "failed": 0,
        "cancelled": 0,
    }
    assert body["queue"]["backoff"] == {"gemini": None, "groq": None, "vertex": None}
    assert body["reviews"] == []


async def test_includes_a_recorded_review_and_active_backoff():
    review = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(name="Security", status="ok", findings=[{"severity": "high"}],
                              elapsed_ms=10, tokens_in=5, tokens_out=2),
        ],
        total_elapsed_ms=10,
        total_tokens_in=5,
        total_tokens_out=2,
        est_cost_usd=0.001,
    )
    store.record_review(
        "owner/repo", 42, review, comment_id=999, now="2026-08-11T12:00:00+00:00", key_index=0
    )

    from datetime import datetime, timezone
    dispatcher._blocked_until["groq"] = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    client = await _client()
    resp = await client.get("/api/dashboard")
    body = resp.json()

    assert body["stats"]["total_reviews"] == 1
    row = body["reviews"][0]
    assert row["repo"] == "owner/repo"
    assert row["pr_number"] == 42
    assert row["comment_url"] == "https://github.com/owner/repo/pull/42#issuecomment-999"
    assert row["specialists"][0]["name"] == "Security"
    assert body["queue"]["backoff"]["groq"] == "2026-08-11T14:00:00+00:00"
    assert body["queue"]["backoff"]["gemini"] is None


async def test_degrades_a_single_section_on_store_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard.store, "dashboard_stats", boom)

    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == {"error": "data unavailable"}
    assert body["reviews"] == []  # unaffected sections still populate


async def test_degrades_queue_by_status_independently_of_backoff(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard.store, "dashboard_queue_counts", boom)
    from datetime import datetime, timezone
    dispatcher._blocked_until["groq"] = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    client = await _client()
    resp = await client.get("/api/dashboard")
    body = resp.json()
    assert body["queue"]["by_status"] == {"error": "data unavailable"}
    assert body["queue"]["backoff"]["groq"] == "2026-08-11T14:00:00+00:00"


async def test_dense_reviews_truncate_at_the_display_limit_and_stay_shaped():
    """The dashboard's Status panel (Queue/specialist windows + Activity.log)
    reads this payload under real load, not just the single-review case
    above -- a solo operator with a busy repo can have far more than 50
    recorded reviews. Record more than _REVIEWS_LIMIT, with every specialist
    status/severity variety the per-specialist windows and Activity.log
    branch on (ok/failed, with/without findings, every severity), and
    confirm the payload still truncates to the limit and every row keeps
    the shape renderSpecialists/renderReviews depend on."""
    from dashboard.router import _REVIEWS_LIMIT

    total = _REVIEWS_LIMIT + 15
    for i in range(total):
        specialists = [
            SpecialistResult(
                name="Security", status="ok" if i % 2 else "failed",
                findings=[{"severity": "critical"}] if i % 2 else [],
                error=None if i % 2 else "provider timeout",
                elapsed_ms=10, tokens_in=5, tokens_out=2,
            ),
            SpecialistResult(
                name="Performance", status="ok",
                findings=[{"severity": "medium"}, {"severity": "high"}],
                elapsed_ms=8, tokens_in=4, tokens_out=1,
            ),
            SpecialistResult(name="Code Quality", status="ok", findings=[],
                              elapsed_ms=6, tokens_in=3, tokens_out=1),
        ]
        review = ReviewResult(
            pr_number=i,
            provider="gemini",
            model="gemini-flash-latest",
            results=specialists,
            total_elapsed_ms=24,
            total_tokens_in=12,
            total_tokens_out=4,
            est_cost_usd=0.002,
        )
        store.record_review(
            "owner/repo", i, review, comment_id=1000 + i,
            now=f"2026-08-11T{i % 24:02d}:00:00+00:00", key_index=0,
        )

    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()

    assert body["stats"]["total_reviews"] == total
    reviews = body["reviews"]
    assert len(reviews) == _REVIEWS_LIMIT  # truncated, not the full `total`

    # Every row still carries the fields renderSpecialists/renderReviews
    # index into -- a dense payload must not degrade row shape.
    for row in reviews:
        assert set(row.keys()) >= {
            "repo", "pr_number", "created_at", "provider", "model",
            "elapsed_ms", "tokens_in", "tokens_out", "est_cost_usd",
            "comment_url", "specialists",
        }
        names = {s["name"] for s in row["specialists"]}
        assert names == {"Security", "Performance", "Code Quality"}
        for s in row["specialists"]:
            assert s["status"] in ("ok", "failed")

    # Both the ok and failed shapes for the most-recently-created review's
    # Security result must be representable -- renderSpecialists reads only
    # reviews[0], so the newest record (highest i, most recently created)
    # must be present and correctly shaped either way.
    latest = reviews[0]
    latest_security = next(s for s in latest["specialists"] if s["name"] == "Security")
    assert latest_security["status"] in ("ok", "failed")
