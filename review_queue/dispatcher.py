"""The single serial consumer of the ticket queue.

It is the ONLY caller of the review path, so all pacing/quota decisions are
serialized. ``blocked_until`` is a per-provider soft gate learned only from
Retry-After (via ReviewRateLimited) so we don't fire calls we know will fail;
it is intentionally in-memory — the durable truth is each ticket's not_before.

Delay handling: a ticket that can't run now is deferred; it also gets a
placeholder comment UNLESS a good review is already visible on the PR
(``_has_visible_review``), in which case a self-cleaning "re-review
scheduled" footnote is shown instead (posted/refreshed by the
``post_pending_notices`` sweep, run once per loop iteration) rather than
staying fully silent. The real result later edits that same comment in
place via the comment marker; claiming a ticket strips any pending
schedule footnote first, since the wait is over regardless of outcome.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from github.IssueComment import IssueComment

import github_app
from config import settings
from formatting import (
    format_failure,
    format_failure_footnote,
    format_placeholder,
    format_schedule_notice,
)
from orchestrator import ReviewRateLimited, ReviewSkipped, attempt_review
from providers import active, active_model, active_vertex_slot, key_index
from providers.active import active_provider
from review_queue import (
    cooldown_config,
    dispatcher_tuning_config,
    review_draft_config,
    store,
    usage_cap_config,
)

logger = logging.getLogger(__name__)


def _jitter(config: dict | None = None) -> float:
    """Injectable jitter source — 0.0 unless dispatcher_backoff_jitter_seconds > 0.

    Kept as a module-level seam so tests monkeypatch it to a constant and the
    whole system stays deterministic; a future multi-instance deployment sets
    the config > 0 to spread retries without a code change.

    ``config`` is the caller's already-validated tuning dict (from
    dispatcher_tuning_config.require_config()). Omitting it re-resolves via
    require_config(), which RAISES rather than KeyError-ing on an empty or
    invalid cache. process_next_due's hard-failure handler always passes its
    already-resolved ``tuning`` explicitly, since raising from inside that
    handler is exactly the bug this parameter exists to prevent.
    """
    tuning = config if config is not None else dispatcher_tuning_config.require_config()
    jitter_max = tuning["dispatcher_backoff_jitter_seconds"]
    if jitter_max <= 0:
        return 0.0
    return random.uniform(0.0, jitter_max)


def compute_backoff(attempts: int, jitter: float = 0.0, config: dict | None = None) -> float:
    """Exponential backoff for a hard-failure retry: min(base*2^(n-1), cap) + jitter.

    ``attempts`` is the 1-based per-ticket hard-failure count (first failure -> base).
    ``config``: see _jitter's docstring -- same contract.
    """
    tuning = config if config is not None else dispatcher_tuning_config.require_config()
    base = tuning["dispatcher_failure_base_backoff_seconds"]
    cap = tuning["dispatcher_failure_max_backoff_seconds"]
    return min(base * 2 ** (attempts - 1), cap) + jitter


_blocked_until: dict[str, datetime] = {}

_IDLE_SLEEP_REFRESH_INTERVAL_SECONDS = 30
_idle_sleep_seconds: float | None = None
_last_idle_sleep_refresh: float = 0.0

# Hardcoded floor for the "tuning config unavailable" deferral, NOT a config
# fallback (same distinction as run_forever's 1.0 idle-sleep floor): the
# value that would tell us how long to wait is exactly the one that's
# missing, so it cannot come from config.
_UNCONFIGURED_DEFER_SECONDS = 60.0


def reset_blocked_until() -> None:
    """Clear the in-memory provider block map (used to isolate tests)."""
    _blocked_until.clear()


def backoff_status() -> dict[str, str]:
    """Snapshot of the in-memory per-provider rate-limit gate, for the
    dashboard. Only providers currently blocked appear; the caller fills in
    a default for every other known provider."""
    return {provider: until.isoformat() for provider, until in _blocked_until.items()}


def _has_visible_review(ticket: store.Ticket) -> bool:
    """True when a prior successful review is already on the PR (worth preserving
    over a placeholder or a bare failure notice). Set only by finalize_review."""
    return ticket.last_reviewed_at is not None


@dataclass
class StepResult:
    action: str  # "idle" | "ran" | "deferred" | "failed"
    ticket_id: int | None = None


async def _post_placeholder(
    repo: str,
    pr: int,
    retry_after: float,
    now: datetime,
    comment_id: int | None = None,
    reason: Literal["provider", "usage_cap"] = "provider",
) -> IssueComment:
    return await asyncio.to_thread(
        github_app.upsert_comment,
        repo,
        pr,
        format_placeholder(pr, retry_after, now, reason=reason),
        comment_id,
    )


def _comment_was_recreated(ticket: store.Ticket, comment: IssueComment | None) -> bool:
    """True when the comment a footnote/notice was just written to is NOT the
    one this ticket had on file -- i.e. the stored comment was confirmed gone
    and github_app had to create a fresh one rather than edit it in place.
    Signals content loss: whatever review body lived in the old comment is
    unrecoverable."""
    return (
        ticket.comment_id is not None
        and comment is not None
        and comment.id != ticket.comment_id
    )


def _installation_confirmed_invalid(exc: Exception) -> bool:
    """True only when discover_and_verify_installation_id's failure is a
    definitive determination (no installation at all, ambiguous multiple
    installations, or a confirmed id mismatch) rather than the check itself
    failing to complete (a transient GitHub-side error -- plausibly the same
    outage that caused the review attempt this is diagnosing to fail in the
    first place, which must never be mistaken for a dead installation).
    Mirrors scripts/deploy.py's check_installation_and_webhook, which
    distinguishes the same two cases the same way: a RuntimeError chained
    from a GithubException (via `raise ... from exc`) is the ambiguous case;
    one raised directly is a real, checked determination."""
    if isinstance(exc, github_app.AppNotInstalledError):
        return True
    return isinstance(exc, RuntimeError) and exc.__cause__ is None


def _check_installation_still_valid_or_die() -> None:
    """Disambiguate a hard review failure: is the whole GitHub App
    installation gone (revoked, or reinstalled under a new id), or just this
    one resource? GITHUB_APP_INSTALLATION_ID is required and never guessed
    on the operator's behalf (ISSUES.md 2026-08-21) -- a long-running process
    must never silently patch its own installation id and keep going under a
    different identity, the same reason main.py's lifespan treats a bad
    installation as a hard startup failure rather than something to work
    around. Confirmed bad -> log loudly and terminate the process (os._exit,
    not a raised exception -- an unhandled exception in a background asyncio
    task is silently dropped, not fatal) so the host platform restarts it and
    re-runs that same loud startup check. An ambiguous/transient failure of
    this check itself must never crash the process -- it just means this was
    an ordinary per-resource error, left to the existing backoff below."""
    try:
        github_app.discover_and_verify_installation_id(settings.github_app_installation_id)
    except Exception as verify_exc:  # noqa: BLE001
        if _installation_confirmed_invalid(verify_exc):
            logger.critical(
                "GitHub App installation is no longer valid (%s) -- terminating so the "
                "host platform restarts and re-verifies at boot", verify_exc,
            )
            os._exit(1)
        logger.exception("installation verification itself failed; treating as transient")


async def post_pending_notices(now: datetime) -> int:
    """Refresh the schedule footnote on up to `dispatcher_notice_sweep_batch_size`
    deferred tickets whose not_before changed since the last notice. Returns the
    count posted. Called once per run_forever iteration, alongside
    process_next_due."""
    posted = 0
    try:
        batch_size = dispatcher_tuning_config.require_config()[
            "dispatcher_notice_sweep_batch_size"
        ]
    except dispatcher_tuning_config.TuningConfigUnavailable as exc:
        # The sweep is cosmetic (it only refreshes a schedule footnote);
        # skipping one iteration is strictly better than either an unbounded
        # query or a guessed batch size. process_next_due's own guard is what
        # makes a genuinely missing/invalid tuning config visible on the PR;
        # this sweep can just wait for the next iteration once it's fixed. On
        # a fully idle queue the cache may legitimately be cold on the very
        # first iteration (process_next_due populates it, and it may not have
        # run yet), so this is not itself an error condition worth escalating.
        logger.warning("skipping notice sweep: %s", exc)
        return 0
    tickets = await asyncio.to_thread(store.tickets_needing_notice, now.isoformat(), batch_size)
    for ticket in tickets:
        try:
            comment = await asyncio.to_thread(
                github_app.append_schedule_notice,
                ticket.repo_full_name,
                ticket.pr_number,
                format_schedule_notice(
                    datetime.fromisoformat(ticket.not_before),
                    # The ticket row is the durable record of WHY this ticket
                    # is waiting -- the sweep runs on a later iteration and
                    # has no other memory of it. NULL means today's original
                    # meaning: a provider rate limit or a cooldown wait.
                    reason=ticket.defer_reason or "provider",
                ),
                ticket.comment_id,
            )
            await asyncio.to_thread(
                store.set_comment_id, ticket.id, comment.id if comment is not None else None
            )
            if _comment_was_recreated(ticket, comment):
                await asyncio.to_thread(store.clear_visible_review, ticket.id)
            await asyncio.to_thread(
                store.mark_notice_posted, ticket.id, ticket.not_before
            )
            posted += 1
        # one ticket's failure must not block the rest
        except Exception:  # noqa: BLE001
            logger.exception("failed to post schedule notice for ticket %s", ticket.id)
    return posted


async def _refresh_slot_config() -> None:
    """Refresh both per-slot model AND vertex project/location from the one
    slot_config table in a single query, same cadence and fail-safe shape as
    the other per-claimed-ticket refreshes. Supersedes the old flat
    _refresh_model_overrides -- see docs/superpowers/specs/2026-09-08-
    slotted-config-and-db-delegation-design.md section 4b."""
    try:
        all_configs = await asyncio.to_thread(store.get_all_slot_configs)
        model_overrides = {
            key: config["model"] for key, config in all_configs.items() if config["model"]
        }
        vertex_overrides = {
            slot: (config["vertex_gcp_project"], config["vertex_gcp_location"])
            for (provider, slot), config in all_configs.items()
            if provider == "vertex"
        }
        active_model.set_override_cache(model_overrides)
        active_vertex_slot.set_override_cache(vertex_overrides)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh slot_config; degrading to no configured slots")
        active_model.reset_override_cache()
        active_vertex_slot.reset_override_cache()


async def _refresh_usage_cap_overrides() -> None:
    """Refresh the usage-cap override once per claimed ticket, same cadence and
    fail-safe shape as the refreshes above. No env fallback (spec section
    10.4): a failed refresh degrades to an empty cache, which
    usage_cap_config.effective_caps() reads as "cap not enforced this tick" --
    not a real default, since there is none left to fall back to."""
    try:
        tokens, reset = await asyncio.to_thread(store.get_usage_cap_overrides)
        usage_cap_config.set_override_cache(tokens, reset)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh usage-cap overrides; cap not enforced this tick")
        usage_cap_config.reset_override_cache()


async def _refresh_review_draft_override() -> None:
    """Refresh the draft-PR review override once per claimed ticket, same
    cadence and fail-safe shape as the refreshes above. No env fallback (spec
    section 10.4): a failed refresh degrades to an empty cache, which
    review_draft_config reads as "no override in force" for this tick."""
    try:
        value = await asyncio.to_thread(store.get_review_draft_override)
        review_draft_config.set_override_cache(value)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh review-draft override; no override this tick")
        review_draft_config.reset_override_cache()


async def _refresh_dispatcher_tuning_config() -> None:
    """Refresh the 9 shared tuning knobs once per claimed ticket, same
    cadence and fail-safe shape as the refreshes above -- a DB-read failure
    must never abort a review, but (unlike provider/key-index, which fall
    back to env/index-0) there is no env default to degrade to:
    reset_override_cache() empties the cache, and process_next_due's own
    guard (dispatcher_tuning_config.require_config()) is what turns that
    empty/invalid cache into a visible, deferred failure rather than
    silently patching over it."""
    try:
        config = await asyncio.to_thread(store.get_dispatcher_tuning_config)
        dispatcher_tuning_config.set_override_cache(config)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh dispatcher tuning config")
        dispatcher_tuning_config.reset_override_cache()


async def process_next_due(now: datetime) -> StepResult:
    """Claim and process one due ticket. Returns what happened.

    action semantics: "deferred" = RateLimited OR a retryable hard failure;
    "failed" = terminal hard-stop; "ran" = completed; "skipped" = an empty
    diff with nothing to review (ticket discarded, no comment posted);
    "idle" = nothing due.
    """
    ticket = await asyncio.to_thread(store.claim_next_due, now.isoformat())
    if ticket is None:
        return StepResult(action="idle")
    logger.info(
        "Claimed ticket %s for %s#%s (attempts=%d)",
        ticket.id,
        ticket.repo_full_name,
        ticket.pr_number,
        ticket.attempts,
    )

    # Refresh the active provider once per claimed ticket, same cadence and
    # fail-safe shape as _refresh_slot_config: provider is DB-only now (no
    # env fallback), so a failed refresh degrades to no configured provider,
    # same as a failed slot_config refresh degrades to no configured slots.
    try:
        provider = await asyncio.to_thread(store.get_provider_override)
        active.set_override_cache(provider)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh the active provider; degrading to unconfigured")
        active.reset_override_cache()

    # Refresh the cooldown override once per claimed ticket, same cadence and
    # fail-safe shape as the provider-override refresh above -- but UNLIKE
    # provider, there is no env fallback here any more (spec section 10.4):
    # a failure degrades to an empty cache, which cooldown_config.
    # effective_cooldown() reads as "not yet available this tick", not a
    # real default.
    try:
        base, cap, factor = await asyncio.to_thread(store.get_cooldown_overrides)
        cooldown_config.set_override_cache(base, cap, factor)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh the cooldown override; unavailable this tick")
        cooldown_config.reset_override_cache()

    # Refresh the API-key-index overrides once per claimed ticket, same
    # cadence and fail-safe shape as the provider/cooldown refreshes above: a
    # failure here must never abort a review, and must never leave a stale
    # cached override in place -- degrade all the way to index 0 for every
    # provider.
    try:
        key_index_overrides = await asyncio.to_thread(store.get_all_key_index_overrides)
        key_index.set_override_cache(key_index_overrides)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh key-index overrides; using index 0")
        key_index.reset_override_cache()

    await _refresh_slot_config()

    await _refresh_usage_cap_overrides()

    await _refresh_review_draft_override()

    await _refresh_dispatcher_tuning_config()

    # Resolved once, up front, and threaded into every read below -- including
    # the hard-failure handler's own compute_backoff/_jitter calls. Discovering
    # a missing/invalid value from inside that handler is what turned a
    # retryable failure into a ticket stuck in 'running' with no comment and
    # no deferral: the exception used to escape past the very code that would
    # have deferred it. A missing tuning config is deferred exactly like a
    # provider rate limit (defer_rate_limited: no attempts increment, no hard
    # stop) since it isn't the ticket's fault.
    try:
        tuning = dispatcher_tuning_config.require_config()
    except dispatcher_tuning_config.TuningConfigUnavailable as exc:
        logger.error(
            "dispatcher tuning config unavailable (%s) -- deferring ticket %s; fix it in the "
            "dashboard config panel or run: uv run python -m scripts.deploy --sync-config-db",
            exc, ticket.id,
        )
        until = now + timedelta(seconds=_UNCONFIGURED_DEFER_SECONDS)
        await asyncio.to_thread(
            store.defer_rate_limited,
            ticket.id,
            not_before=until.isoformat(),
            now=now.isoformat(),
        )
        if not _has_visible_review(ticket):
            comment = await _post_placeholder(
                ticket.repo_full_name,
                ticket.pr_number,
                _UNCONFIGURED_DEFER_SECONDS,
                now,
                ticket.comment_id,
                reason="config",
            )
            await asyncio.to_thread(
                store.set_comment_id, ticket.id, comment.id if comment is not None else None
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    if ticket.notice_not_before is not None:
        try:
            comment = await asyncio.to_thread(
                github_app.clear_schedule_notice,
                ticket.repo_full_name, ticket.pr_number, ticket.comment_id,
            )
            await asyncio.to_thread(
                store.set_comment_id, ticket.id, comment.id if comment is not None else None
            )
            # Unlike append_schedule_notice/append_review_footnote,
            # clear_schedule_notice never creates a fallback comment -- it
            # returns None outright when the bot's comment is confirmed
            # gone, which is why that case can't be detected via
            # _comment_was_recreated (which requires a non-None comment).
            # Both signal the same unrecoverable content loss and must be
            # treated identically to the other comment-touching call sites.
            if comment is None and ticket.comment_id is not None:
                await asyncio.to_thread(store.clear_visible_review, ticket.id)
            elif _comment_was_recreated(ticket, comment):
                await asyncio.to_thread(store.clear_visible_review, ticket.id)
            await asyncio.to_thread(store.clear_notice, ticket.id)
        # a stale note is cosmetic; must not block the review
        except Exception:  # noqa: BLE001
            logger.exception("failed to clear schedule notice for ticket %s", ticket.id)

    # Gate on the ACTIVE provider (the DB override when set, else the
    # env-configured default), not the provider recorded on the ticket at
    # enqueue time — attempt_review always runs against whatever provider is
    # active now, so that's what can be blocked or capped. Resolved once here
    # and shared by both gates below.
    provider = active_provider()

    # Pre-flight cap: has this (provider, key slot) already spent its
    # self-imposed daily budget? Checked BEFORE the review, never predicted:
    # a review's real usage is only known once it completes, so the cap bounds
    # when the NEXT review may start, not the exact daily total — the same
    # shape the reactive-429 gate below already has.
    #
    # FAILS OPEN. Every other per-ticket refresh above degrades to its safe
    # default on error; the safe default for a usage cap is "not enforced",
    # because a broken usage query must never be able to block every review.
    # That is why the whole computation — bucket, query, comparison, reset
    # instant — sits inside one try, and why nothing outside it is read.
    cap_reset_at: datetime | None = None
    try:
        token_cap, reset_time = usage_cap_config.effective_caps()
        if token_cap is not None:
            bucket_start = store.usage_bucket_start(now, reset_time)
            tokens = await asyncio.to_thread(
                store.get_key_usage,
                provider,
                key_index.active_key_index(provider),
                bucket_start.isoformat(),
            )
            if tokens >= token_cap:
                cap_reset_at = bucket_start + timedelta(hours=24)
    except Exception:  # noqa: BLE001
        logger.exception("failed to check key usage cap; proceeding without it")
        cap_reset_at = None

    if cap_reset_at is not None:
        await asyncio.to_thread(
            store.defer_usage_capped,
            ticket.id,
            not_before=cap_reset_at.isoformat(),
            now=now.isoformat(),
        )
        if not _has_visible_review(ticket):
            comment = await _post_placeholder(
                ticket.repo_full_name,
                ticket.pr_number,
                (cap_reset_at - now).total_seconds(),
                now,
                ticket.comment_id,
                reason="usage_cap",
            )
            await asyncio.to_thread(
                store.set_comment_id, ticket.id, comment.id if comment is not None else None
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    blocked = _blocked_until.get(provider)
    if blocked is not None and now < blocked:
        await asyncio.to_thread(
            store.defer_rate_limited,
            ticket.id,
            not_before=blocked.isoformat(),
            now=now.isoformat(),
        )
        if not _has_visible_review(ticket):
            comment = await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now,
                ticket.comment_id,
            )
            await asyncio.to_thread(
                store.set_comment_id, ticket.id, comment.id if comment is not None else None
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    try:
        outcome = await attempt_review(
            ticket.repo_full_name, ticket.pr_number, comment_id=ticket.comment_id
        )
    # hard failure: back off per-ticket, hard-stop at the cap
    except Exception as exc:  # noqa: BLE001
        logger.exception("review attempt failed for ticket %s", ticket.id)
        await asyncio.to_thread(_check_installation_still_valid_or_die)
        next_attempt = ticket.attempts + 1
        max_failure_attempts = tuning["dispatcher_max_failure_attempts"]
        if next_attempt >= max_failure_attempts:
            comment_lost = False
            try:
                if _has_visible_review(ticket):
                    # Preserve the good review; append a self-cleaning footnote.
                    comment = await asyncio.to_thread(
                        github_app.append_review_footnote,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure_footnote(next_attempt),
                        ticket.comment_id,
                    )
                    comment_lost = _comment_was_recreated(ticket, comment)
                else:
                    # No good review to preserve — the notice takes the marker comment.
                    comment = await asyncio.to_thread(
                        github_app.upsert_comment,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure(ticket.pr_number, next_attempt),
                        ticket.comment_id,
                    )
                await asyncio.to_thread(
                    store.set_comment_id,
                    ticket.id,
                    comment.id if comment is not None else None,
                )
                if comment_lost:
                    # The bot's comment was confirmed deleted -- the review
                    # body that lived in it is unrecoverable. Stop claiming a
                    # review is still visible so scheduling/placeholder
                    # decisions reflect reality.
                    await asyncio.to_thread(store.clear_visible_review, ticket.id)
            # couldn't post the notice; don't strand as terminal
            except Exception:  # noqa: BLE001
                logger.exception("failed to post terminal failure notice for ticket %s", ticket.id)
                # The first notice-post attempt happens at next_attempt ==
                # dispatcher_max_failure_attempts (the original hard-stop), so
                # this is the (next_attempt - dispatcher_max_failure_attempts + 1)-th
                # posting attempt. Give up once that count reaches
                # dispatcher_max_notice_post_attempts -- not dispatcher_max_notice_post_attempts
                # tries *on top of* the hard-stop attempt, which is what a plain
                # `+` here previously allowed (dispatcher_max_notice_post_attempts + 1 tries).
                notice_post_ceiling = (
                    tuning["dispatcher_max_failure_attempts"]
                    + tuning["dispatcher_max_notice_post_attempts"]
                    - 2
                )
                if next_attempt > notice_post_ceiling:
                    # The notice itself has now failed to post
                    # dispatcher_max_notice_post_attempts times in a row on top
                    # of the original hard-stop -- retrying forever would be an
                    # unbounded retry loop for what is evidently a persistent
                    # failure (not transient). Give up on the notice and go
                    # terminal anyway: a lost notice is strictly better than
                    # looping forever.
                    await asyncio.to_thread(
                        store.mark_failed,
                        ticket.id,
                        now=now.isoformat(),
                        error=str(exc),
                    )
                    return StepResult(action="failed", ticket_id=ticket.id)
                backoff = compute_backoff(next_attempt, _jitter(tuning), tuning)
                await asyncio.to_thread(
                    store.defer_failed,
                    ticket.id,
                    not_before=(now + timedelta(seconds=backoff)).isoformat(),
                    now=now.isoformat(),
                )
                return StepResult(action="deferred", ticket_id=ticket.id)
            await asyncio.to_thread(
                store.mark_failed, ticket.id, now=now.isoformat(), error=str(exc)
            )
            return StepResult(action="failed", ticket_id=ticket.id)
        backoff = compute_backoff(next_attempt, _jitter(tuning), tuning)
        until = now + timedelta(seconds=backoff)
        await asyncio.to_thread(
            store.defer_failed,
            ticket.id,
            not_before=until.isoformat(),
            now=now.isoformat(),
        )
        return StepResult(action="deferred", ticket_id=ticket.id)

    if isinstance(outcome, ReviewSkipped):
        await asyncio.to_thread(store.discard_skipped_ticket, ticket.id, now.isoformat())
        return StepResult(action="skipped", ticket_id=ticket.id)

    if isinstance(outcome, ReviewRateLimited):
        wait = max(
            outcome.retry_after,
            tuning["dispatcher_min_retry_after_seconds"],
        )
        until = now + timedelta(seconds=wait)
        _blocked_until[provider] = until
        await asyncio.to_thread(
            store.defer_rate_limited,
            ticket.id,
            not_before=until.isoformat(),
            now=now.isoformat(),
        )
        if not _has_visible_review(ticket):
            comment = await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, wait, now, ticket.comment_id
            )
            await asyncio.to_thread(
                store.set_comment_id, ticket.id, comment.id if comment is not None else None
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    level = ticket.cooldown_level
    rereview_not_before = (
        now + timedelta(seconds=store.effective_cooldown(level))
    ).isoformat()
    await asyncio.to_thread(
        store.finalize_review,
        ticket.id,
        now=now.isoformat(),
        rereview_not_before=rereview_not_before,
        rereview_cooldown_level=store.next_cooldown_level(level),
        comment_id=outcome.comment_id,
    )
    return StepResult(action="ran", ticket_id=ticket.id)


async def run_forever() -> None:
    """Production loop: drain the queue, idling when empty. Thin wrapper over
    process_next_due (which holds the tested logic).

    Sleeps ``DISPATCHER_IDLE_SLEEP_SECONDS`` after EVERY iteration, not only
    "idle" ones. A "deferred"/"failed" step can otherwise fire again with
    zero delay (e.g. a ``Retry-After: 0`` or already-past HTTP-date response),
    hammering the same doomed call in a tight loop — the exact 429-hammering
    pattern that has already gotten a provider account-level blocked on this
    project (see CLAUDE.md). This floor is a blunt but robust backstop that
    also defends any future fast-loop path.

    This is the one DB-only value that can't rely on process_next_due's
    per-claimed-ticket refresh (Task 5/9's ``_refresh_*`` calls): that
    refresh never fires while the queue is idle, which is exactly when this
    value matters. So it gets its own throttled refresh here, re-querying
    at most once every ``_IDLE_SLEEP_REFRESH_INTERVAL_SECONDS`` regardless
    of how short the sleep value itself is -- see
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-
    design.md section 6. No env fallback: a failed refresh reuses the last
    known value, and before any refresh has ever succeeded, ``1.0`` is a
    hardcoded floor to avoid busy-looping, not a config fallback.
    """
    while True:
        try:
            await process_next_due(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        # the dispatcher must never die on one ticket
        except Exception:  # noqa: BLE001
            logger.exception("dispatcher step failed")
        try:
            await post_pending_notices(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        # notice sweep failure must not stop the loop
        except Exception:  # noqa: BLE001
            logger.exception("notice sweep failed")
        global _idle_sleep_seconds, _last_idle_sleep_refresh
        now_monotonic = time.monotonic()
        if now_monotonic - _last_idle_sleep_refresh >= _IDLE_SLEEP_REFRESH_INTERVAL_SECONDS:
            try:
                _idle_sleep_seconds = await asyncio.to_thread(store.get_idle_sleep_seconds)
            except Exception:  # noqa: BLE001
                logger.exception("failed to refresh idle-sleep-seconds; reusing last known value")
            _last_idle_sleep_refresh = now_monotonic
        await asyncio.sleep(_idle_sleep_seconds if _idle_sleep_seconds is not None else 1.0)
