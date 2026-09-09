"""Tests for orchestrator.py — asyncio.gather fan-out across all three
specialists (step 6), including partial-failure resilience.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings
from providers import active, active_model
from review_queue import dispatcher_tuning_config
from specialists.schemas import SpecialistResult


@pytest.fixture(autouse=True)
def _default_active_model():
    """orchestrator._active_model() (via get_provider()/_active_model at the
    end of attempt_review) is DB-only, no env fallback (Task 7) -- seeded
    here to slot 0 for all three providers so tests not specifically
    exercising model resolution don't need their own setup. A test that
    cares about a specific model overrides this via active_model.set_override_cache."""
    active_model.set_override_cache({
        ("groq", 0): "llama-3.3-70b-versatile",
        ("gemini", 0): "gemini-flash-latest",
        ("vertex", 0): "gemini-2.5-flash",
    })
    yield
    active_model.reset_override_cache()


@pytest.fixture(autouse=True)
def _tuning_config():
    """orchestrator.attempt_review reads llm_request_timeout_seconds/
    dispatcher_default_retry_after_seconds from dispatcher_tuning_config's
    DB-refreshed cache (no env fallback) -- this file calls attempt_review/
    run_review directly, bypassing the dispatcher's own per-claimed-ticket
    refresh, so the cache needs seeding here instead."""
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


def _ok_result(name: str, tokens_in: int = 10, tokens_out: int = 5) -> SpecialistResult:
    return SpecialistResult(
        name=name,
        status="ok",
        findings=[],
        elapsed_ms=1,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


async def test_run_review_runs_all_three_specialists_and_posts_comment(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="raw diff text", repo_full_name=repo, draft=False),
    )

    posted = {}

    def fake_upsert(repo, pr, body, comment_id=None):
        posted["repo"] = repo
        posted["pr"] = pr
        posted["body"] = body
        posted["comment_id_in"] = comment_id
        return SimpleNamespace(id=111)

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def fake_security(annotated_diff, **kwargs):
        return _ok_result("Security", tokens_in=10, tokens_out=5)

    async def fake_performance(annotated_diff, **kwargs):
        return _ok_result("Performance", tokens_in=8, tokens_out=4)

    async def fake_quality(annotated_diff, **kwargs):
        return _ok_result("Code Quality", tokens_in=6, tokens_out=3)

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(active, "_override", "groq")

    result = await orchestrator.run_review("owner/repo", 99)

    assert result.pr_number == 99
    assert len(result.results) == 3
    assert {r.name for r in result.results} == {"Security", "Performance", "Code Quality"}
    assert result.total_tokens_in == 24
    assert result.total_tokens_out == 12

    assert posted["repo"] == "owner/repo"
    assert posted["pr"] == 99
    assert "PR #99" in posted["body"]
    assert posted["comment_id_in"] is None   # run_review never threads a comment_id


async def test_run_review_survives_one_specialist_raising(monkeypatch):
    """A specialist coroutine that raises (bypassing its own internal
    never-raise contract, e.g. a genuine bug) must not blank the comment or
    drop the other two specialists' results — SPEC's core resilience
    guarantee, enforced at the orchestrator's gather/merge layer too.
    """
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="raw diff text", repo_full_name=repo, draft=False),
    )

    posted = {}

    def fake_upsert(repo, pr, body, comment_id=None):
        posted["body"] = body
        posted["comment_id_in"] = comment_id
        return SimpleNamespace(id=222)

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def fake_security(annotated_diff, **kwargs):
        return _ok_result("Security")

    async def fake_performance(annotated_diff, **kwargs):
        raise RuntimeError("boom")

    async def fake_quality(annotated_diff, **kwargs):
        return _ok_result("Code Quality")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(active, "_override", "groq")

    result = await orchestrator.run_review("owner/repo", 1)

    assert len(result.results) == 3
    by_name = {r.name: r for r in result.results}
    assert by_name["Security"].status == "ok"
    assert by_name["Code Quality"].status == "ok"
    assert by_name["Performance"].status == "failed"
    assert "boom" in by_name["Performance"].error

    assert "❌ Performance check failed" in posted["body"]
    assert "Security" in posted["body"]
    assert "Code Quality" in posted["body"]
    assert posted["comment_id_in"] is None   # run_review never threads a comment_id


async def test_attempt_review_raises_when_every_specialist_fails(monkeypatch):
    """If all three specialists fail (e.g. a misconfigured key-index override
    makes factory._build raise for every one), the review must NOT be posted
    as a successful comment or finalized as done -- it must propagate so the
    dispatcher's existing retry/backoff/terminal-failure-notice machinery
    handles it exactly like any other hard failure."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="raw diff text", repo_full_name=repo, draft=False),
    )

    posted = {}

    def fake_upsert(repo, pr, body, comment_id=None):
        posted["called"] = True
        return SimpleNamespace(id=333)

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def fake_security(annotated_diff, **kwargs):
        raise ValueError("no credential configured for provider=groq index=0")

    async def fake_performance(annotated_diff, **kwargs):
        raise ValueError("no credential configured for provider=groq index=0")

    async def fake_quality(annotated_diff, **kwargs):
        raise ValueError("no credential configured for provider=groq index=0")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(active, "_override", "groq")

    try:
        await orchestrator.attempt_review("owner/repo", 1)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True

    assert raised, "attempt_review must raise when every specialist failed"
    assert "called" not in posted, "no comment must be posted for a total failure"


async def test_run_review_reflects_active_model_per_provider(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name=repo, draft=False),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment", lambda *a, **k: SimpleNamespace(id=1)
    )

    async def ok(name):
        async def _inner(annotated_diff, **kwargs):
            return _ok_result(name)

        return _inner

    monkeypatch.setattr(orchestrator, "run_security_specialist", await ok("Security"))
    monkeypatch.setattr(orchestrator, "run_performance_specialist", await ok("Performance"))
    monkeypatch.setattr(orchestrator, "run_quality_specialist", await ok("Code Quality"))

    monkeypatch.setattr(active, "_override", "groq")
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    result = await orchestrator.run_review("owner/repo", 1)
    assert result.model == "llama-3.3-70b-versatile"

    monkeypatch.setattr(active, "_override", "gemini")
    monkeypatch.setattr(settings, "gemini_model", "gemini-flash-latest")
    result = await orchestrator.run_review("owner/repo", 1)
    assert result.model == "gemini-flash-latest"


async def test_run_review_records_the_completed_review(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="raw diff text", repo_full_name=repo, draft=False),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=111),
    )

    async def fake_security(annotated_diff, **kwargs):
        return _ok_result("Security")

    async def fake_performance(annotated_diff, **kwargs):
        return _ok_result("Performance")

    async def fake_quality(annotated_diff, **kwargs):
        return _ok_result("Code Quality")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(active, "_override", "groq")

    recorded = {}

    def fake_record_review(repo_full_name, pr_number, review, comment_id, now, key_index):
        recorded["repo_full_name"] = repo_full_name
        recorded["pr_number"] = pr_number
        recorded["review"] = review
        recorded["comment_id"] = comment_id
        recorded["now"] = now
        recorded["key_index"] = key_index

    monkeypatch.setattr(orchestrator.store, "record_review", fake_record_review)

    result = await orchestrator.run_review("owner/repo", 99)

    assert recorded["repo_full_name"] == "owner/repo"
    assert recorded["pr_number"] == 99
    assert recorded["review"] is result
    assert recorded["comment_id"] == 111
    assert recorded["now"]  # a non-empty ISO timestamp string
    assert recorded["key_index"] == 0     # no override cached -> the base slot


async def test_run_review_survives_record_review_raising(monkeypatch):
    """A dashboard-persistence failure must never fail an otherwise-successful
    review — the PR comment is already posted by this point."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="raw diff text", repo_full_name=repo, draft=False),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=111),
    )

    async def fake_security(annotated_diff, **kwargs):
        return _ok_result("Security")

    async def fake_performance(annotated_diff, **kwargs):
        return _ok_result("Performance")

    async def fake_quality(annotated_diff, **kwargs):
        return _ok_result("Code Quality")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(active, "_override", "groq")

    def boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(orchestrator.store, "record_review", boom)

    result = await orchestrator.run_review("owner/repo", 99)  # must not raise
    assert result.pr_number == 99


async def test_attempt_review_migrates_a_renamed_repo(monkeypatch):
    """fetch_pr_diff surfaces GitHub's canonical repo name for free (already
    resolved internally) -- when it differs from what was requested, the
    repo was renamed, and attempt_review must migrate the DB rows rather
    than silently keep using the stale name."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name="owner/renamed", draft=False),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=1),
    )

    async def ok(_, **kwargs):
        return _ok_result("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)
    monkeypatch.setattr(active, "_override", "groq")

    migrated = {}

    def fake_migrate(old, new, now):
        migrated["old"] = old
        migrated["new"] = new

    monkeypatch.setattr(orchestrator.store, "migrate_repo_rename", fake_migrate)

    await orchestrator.attempt_review("owner/old-name", 1)

    assert migrated == {"old": "owner/old-name", "new": "owner/renamed"}


async def test_attempt_review_does_not_migrate_when_name_is_unchanged(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name=repo, draft=False),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=1),
    )

    async def ok(_, **kwargs):
        return _ok_result("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)
    monkeypatch.setattr(active, "_override", "groq")

    def boom(*a, **k):
        raise AssertionError("must not migrate when the name didn't change")

    monkeypatch.setattr(orchestrator.store, "migrate_repo_rename", boom)

    await orchestrator.attempt_review("owner/repo", 1)  # must not raise


async def test_attempt_review_survives_migrate_repo_rename_raising(monkeypatch):
    """A migration hiccup must never fail an otherwise-successful review --
    same guarantee as the existing record_review failure isolation."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="diff", repo_full_name="owner/renamed", draft=False),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=1),
    )

    async def ok(_, **kwargs):
        return _ok_result("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)
    monkeypatch.setattr(active, "_override", "groq")

    def boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(orchestrator.store, "migrate_repo_rename", boom)

    outcome = await orchestrator.attempt_review("owner/old-name", 1)  # must not raise
    assert outcome.review.pr_number == 1


async def test_attempt_review_skips_entirely_on_an_empty_diff(monkeypatch):
    """An empty diff (e.g. an empty merge commit) must short-circuit before
    any specialist call, comment post, or dashboard record -- ISSUES.md's
    'Empty diffs still fan out all 3 specialists' gap."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="", repo_full_name=repo, draft=False),
    )

    def boom(*a, **k):
        raise AssertionError("must not be called for an empty diff")

    monkeypatch.setattr(orchestrator, "run_security_specialist", boom)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", boom)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", boom)
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", boom)
    monkeypatch.setattr(orchestrator.store, "record_review", boom)

    outcome = await orchestrator.attempt_review("owner/repo", 1)

    assert isinstance(outcome, orchestrator.ReviewSkipped)


async def test_attempt_review_skips_a_draft_pr_when_the_override_disallows_it(monkeypatch):
    """ISSUES.md's 'Draft PRs are reviewed identically to ready-for-review
    PRs' gap: a draft PR must short-circuit before any specialist call,
    comment post, or dashboard record, same as an empty diff."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="real diff content", repo_full_name=repo, draft=True),
    )
    monkeypatch.setattr(
        orchestrator.review_draft_config, "effective_review_draft_prs", lambda: False
    )

    def boom(*a, **k):
        raise AssertionError("must not be called for a draft PR")

    monkeypatch.setattr(orchestrator, "run_security_specialist", boom)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", boom)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", boom)
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", boom)
    monkeypatch.setattr(orchestrator.store, "record_review", boom)

    outcome = await orchestrator.attempt_review("owner/repo", 1)

    assert isinstance(outcome, orchestrator.ReviewSkipped)


async def test_attempt_review_reviews_a_draft_pr_when_the_override_allows_it(monkeypatch):
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="real diff content", repo_full_name=repo, draft=True),
    )
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=1),
    )
    monkeypatch.setattr(
        orchestrator.review_draft_config, "effective_review_draft_prs", lambda: True
    )

    async def ok(_, **kwargs):
        return _ok_result("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)
    monkeypatch.setattr(active, "_override", "groq")

    outcome = await orchestrator.attempt_review("owner/repo", 1)

    assert isinstance(outcome, orchestrator.ReviewCompleted)


async def test_attempt_review_raises_for_a_draft_pr_when_the_override_is_unrefreshed(monkeypatch):
    """review_draft_config.effective_review_draft_prs() returning None means
    the DB-only config hasn't been refreshed yet (Task 6, no env fallback) --
    this must surface as a real failure, not silently skip or silently
    review the draft."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="real diff content", repo_full_name=repo, draft=True),
    )
    monkeypatch.setattr(
        orchestrator.review_draft_config, "effective_review_draft_prs", lambda: None
    )

    with pytest.raises(RuntimeError, match="review-draft-PRs config not available"):
        await orchestrator.attempt_review("owner/repo", 1)


async def test_attempt_review_still_migrates_a_rename_on_an_empty_diff(monkeypatch):
    """The rename check is cheap and orthogonal to diff content -- it must
    still run even when the diff itself turns out to be empty."""
    import orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.github_app, "fetch_pr_diff",
        lambda repo, pr: SimpleNamespace(text="", repo_full_name="owner/renamed", draft=False),
    )

    migrated = {}

    def fake_migrate(old, new, now):
        migrated["old"] = old
        migrated["new"] = new

    monkeypatch.setattr(orchestrator.store, "migrate_repo_rename", fake_migrate)

    outcome = await orchestrator.attempt_review("owner/old-name", 1)

    assert isinstance(outcome, orchestrator.ReviewSkipped)
    assert migrated == {"old": "owner/old-name", "new": "owner/renamed"}


def test_active_model_resolves_per_provider_through_the_registry():
    import orchestrator
    from providers import active

    active_model.set_override_cache({
        ("gemini", 0): "model-gemini",
        ("groq", 0): "model-groq",
        ("vertex", 0): "model-vertex",
    })
    for provider, expected in (
        ("gemini", "model-gemini"),
        ("groq", "model-groq"),
        ("vertex", "model-vertex"),
    ):
        active.set_override_cache(provider)
        assert orchestrator._active_model() == expected
    active.reset_override_cache()
