# tests/test_orchestrator_rate_limited.py
"""attempt_review distinguishes a rate-limited review (defer, no comment) from a
completed one (post comment). A non-quota specialist error still COMPLETES with a
visible failed row — only a real 429 makes the whole review rate-limited.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from providers import active, active_model
from providers.base import RateLimited
from review_queue import dispatcher_tuning_config
from specialists.schemas import SpecialistResult


def _ok(name):
    return SpecialistResult(
        name=name, status="ok", findings=[], elapsed_ms=1, tokens_in=1, tokens_out=1
    )


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr(active, "_override", "groq")


@pytest.fixture(autouse=True)
def _default_active_model():
    """See tests/test_orchestrator.py's identical fixture docstring."""
    active_model.set_override_cache({("groq", 0): "llama-3.3-70b-versatile"})
    yield
    active_model.reset_override_cache()


@pytest.fixture(autouse=True)
def _tuning_config():
    """See tests/test_orchestrator.py's identical fixture docstring: this
    file also calls attempt_review/run_review directly, bypassing the
    dispatcher's own per-claimed-ticket refresh."""
    dispatcher_tuning_config.set_override_cache({
        "llm_request_timeout_seconds": 45.0,
        "dispatcher_default_retry_after_seconds": 60.0,
        "dispatcher_failure_base_backoff_seconds": 2.0,
        "dispatcher_failure_max_backoff_seconds": 300.0,
        "dispatcher_max_failure_attempts": 5,
        "dispatcher_max_notice_post_attempts": 3,
        "dispatcher_min_retry_after_seconds": 1.0,
        "dispatcher_backoff_jitter_seconds": 0.0,
        "dispatcher_notice_sweep_batch_size": 20,
    })
    yield
    dispatcher_tuning_config.reset_override_cache()


async def test_attempt_review_returns_rate_limited_and_posts_nothing(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name=repo, draft=False),
    )
    posted = []
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: posted.append(a))

    async def sec(_, **kwargs):
        return _ok("Security")

    async def perf(_, **kwargs):
        raise RateLimited(30.0)

    async def qual(_, **kwargs):
        raise RateLimited(45.0)

    monkeypatch.setattr(orchestrator, "run_security_specialist", sec)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", perf)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", qual)

    outcome = await orchestrator.attempt_review("owner/repo", 1)

    assert isinstance(outcome, orchestrator.ReviewRateLimited)
    assert outcome.retry_after == 45.0  # max of the two
    assert posted == []                 # no comment on a rate-limited review


async def test_attempt_review_completes_and_posts_when_ok(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name=repo, draft=False),
    )
    posted = {}

    def fake_upsert(repo, pr, body, comment_id=None):
        posted["body"] = body
        posted["comment_id_in"] = comment_id
        return SimpleNamespace(id=222)

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def mk(name):
        async def _inner(_, **kwargs):
            return _ok(name)
        return _inner

    monkeypatch.setattr(orchestrator, "run_security_specialist", await mk("Security"))
    monkeypatch.setattr(orchestrator, "run_performance_specialist", await mk("Performance"))
    monkeypatch.setattr(orchestrator, "run_quality_specialist", await mk("Code Quality"))

    outcome = await orchestrator.attempt_review("owner/repo", 2, comment_id=555)

    assert isinstance(outcome, orchestrator.ReviewCompleted)
    assert outcome.review.pr_number == 2
    assert "PR #2" in posted["body"]
    assert posted["comment_id_in"] == 555   # incoming id threaded to the post
    assert outcome.comment_id == 222         # posted comment's id captured


async def test_run_review_raises_on_rate_limited(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name=repo, draft=False),
    )
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: None)

    async def rl(_, **kwargs):
        raise RateLimited(12.0)

    async def ok(_, **kwargs):
        return _ok("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", rl)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)

    with pytest.raises(RateLimited):
        await orchestrator.run_review("owner/repo", 3)


async def test_run_specialist_lets_rate_limited_escape(monkeypatch):
    """run_specialist normally never raises — but RateLimited MUST escape so the
    orchestrator can defer instead of rendering a failed row."""
    import specialists.base as base

    class FakeProvider:
        async def complete(self, system, user, schema, **kwargs):
            raise RateLimited(20.0)

    monkeypatch.setattr(base, "get_provider", lambda: FakeProvider())

    from specialists.security import SecurityFindings, SECURITY_SYSTEM_PROMPT

    with pytest.raises(RateLimited):
        await base.run_specialist(
            name="Security",
            annotated_diff="diff",
            system_prompt=SECURITY_SYSTEM_PROMPT,
            container_schema=SecurityFindings,
            timeout_seconds=45.0,
            default_retry_after_seconds=60.0,
        )
