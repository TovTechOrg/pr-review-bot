import inspect

from review_queue import store as real_store
from demo import store as demo_store


def _public(module) -> set[str]:
    return {
        name for name, obj in vars(module).items()
        if not name.startswith("_") and inspect.isfunction(obj)
        and obj.__module__ == module.__name__
    }


def test_mock_covers_the_real_store_public_surface():
    """The demo breaks silently when the real store grows a function.

    This is the guard: the failure would otherwise surface to a stranger
    following a link from an article, months after the change.
    """
    missing = _public(real_store) - _public(demo_store)
    assert not missing, f"demo/store.py is missing: {sorted(missing)}"


def test_enqueue_then_claim_returns_the_ticket():
    demo_store.reset()
    demo_store.enqueue_or_update(
        repo_full_name="bot-demo/example-app", pr_number=7,
        head_sha="abc", provider="groq", now="2026-09-16T00:00:00+00:00",
    )
    ticket = demo_store.claim_next_due("2026-09-16T00:00:01+00:00")
    assert ticket is not None
    assert ticket.pr_number == 7
    assert demo_store.claim_next_due("2026-09-16T00:00:02+00:00") is None


def test_boot_config_getters_are_valid():
    demo_store.reset()
    assert demo_store.get_provider_override() in {"gemini", "vertex", "groq"}
    provider = demo_store.get_provider_override()
    assert demo_store.get_slot_config(provider, 0) is not None
    assert all(v is not None for v in demo_store.get_cooldown_overrides())
    assert demo_store.get_dispatcher_tuning_config()


def test_dashboard_stats_on_empty_store():
    demo_store.reset()
    assert demo_store.dashboard_stats() == {
        "total_reviews": 0,
        "total_cost_usd": 0.0,
        "avg_elapsed_ms": 0,
    }


class _FakeReview:
    def __init__(self, elapsed_ms, cost, tokens_in=100, tokens_out=200):
        self.provider = "groq"
        self.model = "fake-model"
        self.total_elapsed_ms = elapsed_ms
        self.total_tokens_in = tokens_in
        self.total_tokens_out = tokens_out
        self.est_cost_usd = cost
        self.results = []


def test_dashboard_stats_after_recording_reviews():
    demo_store.reset()
    demo_store.record_review(
        "bot-demo/example-app", 1, _FakeReview(1000, 0.001234),
        None, "2026-09-16T00:00:00+00:00", 0,
    )
    demo_store.record_review(
        "bot-demo/example-app", 2, _FakeReview(2000, 0.002345),
        None, "2026-09-16T00:01:00+00:00", 0,
    )
    stats = demo_store.dashboard_stats()
    assert stats["total_reviews"] == 2
    assert stats["total_cost_usd"] == round(0.001234 + 0.002345, 4)
    assert stats["avg_elapsed_ms"] == 1500


def test_dashboard_queue_counts_zero_fills_all_known_statuses():
    demo_store.reset()
    assert demo_store.dashboard_queue_counts() == {
        "pending": 0,
        "running": 0,
        "deferred": 0,
        "retrying": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
    }


def test_one_session_never_sees_anothers_review():
    from demo.session import current_session
    from specialists.schemas import ReviewResult

    review = ReviewResult(
        pr_number=7, provider="groq", model="llama-3.3-70b-versatile",
        results=[], total_elapsed_ms=10, total_tokens_in=1, total_tokens_out=1,
        est_cost_usd=0.0001,
    )

    demo_store.reset()
    current_session.set("reader-a")
    demo_store.record_review(
        "bot-demo/example-app", 7, review, 1001, "2026-09-16T00:00:00+00:00", 0,
    )

    current_session.set("reader-b")
    assert demo_store.dashboard_reviews() == []

    current_session.set("reader-a")
    visible = demo_store.dashboard_reviews()
    assert len(visible) == 1
    assert visible[0]["repo"] == "bot-demo/example-app"
    assert "_session" not in visible[0], "the tag is internal, never rendered"
