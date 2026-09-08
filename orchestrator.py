"""Fetch diff -> run specialists -> merge into ReviewResult -> post PR comment.

Runs Security, Performance, and Code Quality concurrently via
``asyncio.gather(..., return_exceptions=True)``. Each specialist's own
``run_specialist()`` (specialists/base.py) already never raises — a bad LLM
response becomes a ``status="failed"`` ``SpecialistResult``, not an
exception. The ``return_exceptions=True`` + merge step below is a second,
belt-and-suspenders layer of the same guarantee: even if a specialist
function raised for a reason outside that contract (a genuine bug, a
cancellation, ...), one specialist's exception can never blank the comment
or drop the other two specialists' results (SPEC.md's core resilience
requirement — "partial failure is always visible").
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import github_app
from diff_utils import annotate_and_cap
from formatting import format_comment
from providers.active import active_provider
from providers.active_model import active_model
from providers.base import RateLimited
from providers.key_index import active_key_index
from providers.pricing import estimate_cost_usd
from review_queue import dispatcher_tuning_config, review_draft_config, store
from specialists.performance import run_performance_specialist
from specialists.quality import run_quality_specialist
from specialists.schemas import ReviewResult, SpecialistResult
from specialists.security import run_security_specialist

logger = logging.getLogger(__name__)

_SPECIALIST_NAMES = ("Security", "Performance", "Code Quality")


def _active_model() -> str:
    """The model name for whichever (provider, slot) is actually active.

    Delegates to providers/active_model.py, the single resolver shared with
    factory.get_provider() -- so the model reported in the PR comment is
    always the model the call actually used. Only ever called after at least
    one specialist has already run against this same (provider, slot) via
    get_provider() (which raises if unconfigured), so a None here would mean
    the cache was reset between that call and this one -- treated as the
    same class of real, visible failure as the original get_provider() raise
    would have been, not silently swallowed.
    """
    provider = active_provider()
    index = active_key_index(provider)
    model = active_model(provider, index)
    if model is None:
        raise ValueError(f"no model configured for provider={provider!r} slot={index}")
    return model


@dataclass
class ReviewCompleted:
    review: ReviewResult
    comment_id: int | None = None


@dataclass
class ReviewRateLimited:
    retry_after: float


@dataclass
class ReviewSkipped:
    """Nothing to review: either the diff has no substantive content (e.g.
    an empty merge commit) or the PR is a draft and review_draft_config says
    drafts aren't reviewed -- no specialist ran, no comment was posted, no
    dashboard row was recorded. The empty-diff case is deliberately narrower
    than the oversized/binary-diff handling already in
    diff_utils.py/github_app.py, which produce real content worth a
    specialist's opinion; this is only the "there is genuinely nothing here"
    case."""


async def attempt_review(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> ReviewCompleted | ReviewRateLimited | ReviewSkipped:
    """Run the full review pipeline once for one PR.

    On completion, posts the Markdown comment via ``github_app.upsert_comment``
    and returns ``ReviewCompleted``. If any specialist call is rate-limited,
    the whole review is atomic: no comment is posted, and the max
    ``retry_after`` across the rate-limited calls is returned via
    ``ReviewRateLimited`` so a caller (e.g. the durable queue) can retry later.
    """
    started = time.monotonic()

    diff = await asyncio.to_thread(github_app.fetch_pr_diff, repo_full_name, pr_number)
    if diff.repo_full_name != repo_full_name:
        # GitHub transparently redirects a renamed repo's old-name requests
        # (no error raised) -- fetch_pr_diff's canonical name is the only
        # signal a rename happened. Best-effort: a migration hiccup must
        # never fail an otherwise-successful review, same guarantee as the
        # record_review call below.
        try:
            await asyncio.to_thread(
                store.migrate_repo_rename,
                repo_full_name,
                diff.repo_full_name,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to migrate renamed repo %s -> %s", repo_full_name, diff.repo_full_name
            )

    if diff.draft:
        review_drafts = review_draft_config.effective_review_draft_prs()
        if review_drafts is None:
            # DB-only, no env fallback (Task 6): a None here means the
            # per-claimed-ticket refresh in dispatcher.py hasn't populated
            # this cache yet, or its last refresh failed -- a real missing-
            # config state, not "treat drafts like non-drafts". Raising
            # routes this through the dispatcher's existing hard-failure
            # retry/backoff path (visible, not silently skipped/discarded)
            # exactly like any other unrefreshed DB-only config.
            raise RuntimeError(
                "review-draft-PRs config not available (not yet refreshed from the DB)"
            )
        if not review_drafts:
            return ReviewSkipped()

    annotated = annotate_and_cap(diff.text)

    if not annotated.text.strip():
        return ReviewSkipped()

    # Sourced once here, not read inside providers/specialists: these two
    # tuning knobs are DB-refreshed once per claimed ticket
    # (review_queue/dispatcher.py::_refresh_dispatcher_tuning_config), and
    # threading them down as call parameters keeps providers/ and
    # specialists/ fully dependency-free from review_queue/ -- see
    # providers/base.py::LLMProvider's docstring for the full reasoning.
    tuning = dispatcher_tuning_config.effective_config()
    timeout_seconds = tuning["llm_request_timeout_seconds"]
    default_retry_after_seconds = tuning["dispatcher_default_retry_after_seconds"]

    # Referencing these as bare module-level names (not a precomputed tuple
    # of function objects) means they resolve at call time, so tests can
    # monkeypatch `orchestrator.run_security_specialist` etc. per-call.
    raw_results = await asyncio.gather(
        run_security_specialist(
            annotated.text,
            timeout_seconds=timeout_seconds,
            default_retry_after_seconds=default_retry_after_seconds,
        ),
        run_performance_specialist(
            annotated.text,
            timeout_seconds=timeout_seconds,
            default_retry_after_seconds=default_retry_after_seconds,
        ),
        run_quality_specialist(
            annotated.text,
            timeout_seconds=timeout_seconds,
            default_retry_after_seconds=default_retry_after_seconds,
        ),
        return_exceptions=True,
    )

    rate_limits = [r.retry_after for r in raw_results if isinstance(r, RateLimited)]
    if rate_limits:
        return ReviewRateLimited(retry_after=max(rate_limits))

    results = [
        outcome
        if isinstance(outcome, SpecialistResult)
        else SpecialistResult(
            name=name, status="failed", findings=[], error=str(outcome), elapsed_ms=0
        )
        for name, outcome in zip(_SPECIALIST_NAMES, raw_results)
    ]

    if all(r.status == "failed" for r in results):
        # Every specialist failed (e.g. a misconfigured key-index override,
        # or the provider itself is down) -- posting this as a "completed"
        # review would finalize the ticket as done and bypass the
        # retry/backoff/terminal-failure-notice machinery entirely, leaving
        # a PR with a comment full of false "completed normally" rows and no
        # further retry until the next push. Raising here routes this
        # through the dispatcher's existing hard-failure handling instead,
        # exactly like any other exception from this function.
        errors = "; ".join(f"{r.name}: {r.error}" for r in results)
        raise RuntimeError(f"all specialists failed: {errors}")

    total_tokens_in = sum(r.tokens_in for r in results)
    total_tokens_out = sum(r.tokens_out for r in results)
    total_elapsed_ms = int((time.monotonic() - started) * 1000)

    provider = active_provider()
    model = _active_model()
    est_cost_usd = estimate_cost_usd(provider, model, total_tokens_in, total_tokens_out)

    review_result = ReviewResult(
        pr_number=pr_number,
        provider=provider,
        model=model,
        results=results,
        total_elapsed_ms=total_elapsed_ms,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        est_cost_usd=est_cost_usd,
        diff_truncated=annotated.truncated,
    )

    body = format_comment(review_result)
    posted = await asyncio.to_thread(
        github_app.upsert_comment, repo_full_name, pr_number, body, comment_id
    )
    try:
        await asyncio.to_thread(
            store.record_review,
            repo_full_name,
            pr_number,
            review_result,
            posted.id,
            datetime.now(timezone.utc).isoformat(),
            active_key_index(provider),
        )
    # a dashboard-persistence failure must never fail an already-posted review
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to record review for the dashboard (repo=%s pr=%s)",
            repo_full_name, pr_number,
        )
    return ReviewCompleted(review=review_result, comment_id=posted.id)


async def run_review(repo_full_name: str, pr_number: int) -> ReviewResult:
    """Back-compat entry point for scripts/tests: returns the ``ReviewResult``
    on completion, raises ``RateLimited`` if the review was rate-limited.
    """
    outcome = await attempt_review(repo_full_name, pr_number)
    if isinstance(outcome, ReviewRateLimited):
        raise RateLimited(outcome.retry_after)
    if isinstance(outcome, ReviewSkipped):
        raise RuntimeError("review skipped: PR diff has no substantive content")
    return outcome.review
