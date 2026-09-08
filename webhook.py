"""GitHub PR webhook route: HMAC verification, delivery dedup, durable enqueue."""

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

import github_app
from config import settings
from hmac_verify import verify_signature
from providers.active import active_provider
from review_queue import store

logger = logging.getLogger(__name__)

# PR actions we react to. GitHub sends the `pull_request` event for many
# actions (closed, labeled, assigned, ...) — per SPEC.md's confirmed
# decision, only these four trigger a review; everything else is a no-op
# except _CANCEL_ACTIONS below. `ready_for_review` fires independent of any
# push -- it's the only way a draft PR marked ready with zero new commits
# still gets a review when review_draft_config skips drafts (whether a PR is
# CURRENTLY a draft is checked live at dispatch time, not from this payload
# -- see orchestrator.attempt_review).
_REVIEW_TRIGGER_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}

# "closed" covers both a merge and a plain close (distinguished only by
# pull_request.merged, which doesn't matter here) -- either way the PR is no
# longer actionable, so any ticket that hasn't started running yet is
# cancelled rather than left to waste a review on it.
_CANCEL_ACTIONS = {"closed"}


def _is_base_retarget(payload: dict) -> bool:
    """True only for an `edited` delivery that changed the PR's base branch.

    GitHub fires `edited` for title/body edits too, which must NOT trigger a
    review -- only a base change can change the effective diff. The payload
    names exactly what changed in `changes`, keyed by field name (e.g.
    `{"title": {"from": "..."}}` vs `{"base": {"ref": {...}, "sha": {...}}}`),
    so checking for a `base` key unambiguously identifies a retarget.
    """
    return "base" in (payload.get("changes") or {})

router = APIRouter()

_DEDUP_CAPACITY = 1000
_seen_deliveries: OrderedDict[str, None] = OrderedDict()


def reset_dedup_cache() -> None:
    """Clear the in-memory delivery-id cache. Used to isolate tests."""
    _seen_deliveries.clear()


def _is_duplicate_delivery(delivery_id: str) -> bool:
    """Check only -- does not mark. Marking is a separate step
    (_mark_delivery_processed), applied only once the delivery has been
    fully and successfully handled -- see that function's docstring for why
    check-and-mark in one step is wrong here."""
    if delivery_id in _seen_deliveries:
        _seen_deliveries.move_to_end(delivery_id)
        return True
    return False


def _mark_delivery_processed(delivery_id: str) -> None:
    """Record a delivery id as done against the bounded LRU dedup cache.

    Deliberately NOT called until _handle_pull_request_payload has returned
    without raising. Marking at check-time (the previous behavior) meant a
    delivery that failed mid-processing (e.g. Postgres briefly unreachable
    during store.enqueue_or_update) was already recorded as seen -- GitHub's
    own Redeliver reuses the same X-GitHub-Delivery id, so the retry would
    hit the dedup cache and return 200 "already processed" while the PR was
    never actually enqueued, with no way to force a real redelivery short of
    restarting the process (which clears the in-memory cache)."""
    _seen_deliveries[delivery_id] = None
    if len(_seen_deliveries) > _DEDUP_CAPACITY:
        _seen_deliveries.popitem(last=False)


async def _handle_pull_request_payload(payload: dict) -> None:
    """React to a `pull_request` webhook payload: enqueue a durable review
    ticket for a triggering action (or an `edited` that retargeted the base
    branch), cancel a queued-but-not-yet-running ticket when the PR closes
    or merges, or no-op for everything else."""
    action = payload.get("action")
    is_base_retarget = action == "edited" and _is_base_retarget(payload)
    logger.info("pull_request webhook: action=%s", action)
    if (
        action not in _REVIEW_TRIGGER_ACTIONS
        and action not in _CANCEL_ACTIONS
        and not is_base_retarget
    ):
        logger.info("Ignoring non-triggering action=%s", action)
        return
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    pr_number = pull_request.get("number")
    if not repo_full_name or pr_number is None:
        logger.warning("pull_request webhook missing repo/pr number; skipping")
        return
    target_repos = settings.target_repos()
    if target_repos and repo_full_name.casefold() not in {r.casefold() for r in target_repos}:
        logger.info(
            "Ignoring webhook for non-target repo %s (target_repos=%s)",
            repo_full_name,
            sorted(target_repos),
        )
        return

    if action in _CANCEL_ACTIONS:
        logger.info("Cancelling ticket for %s#%s", repo_full_name, pr_number)
        cancelled = await asyncio.to_thread(
            store.cancel_ticket,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            now=datetime.now(timezone.utc).isoformat(),
        )
        # A cancelled ticket is never claimed again, so this is the only
        # chance to strip a still-live schedule-notice footnote -- otherwise
        # it sits on the closed/merged PR's comment forever (tickets_needing_
        # notice/clear_notice only ever look at 'deferred' tickets, and this
        # one just left that status). Cosmetic only; must not block on it.
        if cancelled is not None and cancelled.notice_not_before is not None:
            try:
                await asyncio.to_thread(
                    github_app.clear_schedule_notice,
                    cancelled.repo_full_name, cancelled.pr_number, cancelled.comment_id,
                )
                await asyncio.to_thread(store.clear_notice, cancelled.id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to clear schedule notice for cancelled ticket %s", cancelled.id
                )
        return

    # Best-effort, immediate "we saw your push" ack -- mirrors CodeRabbit's
    # own eyes-reaction. The actual review is dispatched separately and can
    # take a while (queue depth, provider backoff), so this is the fast
    # signal a human watches for; a failure here must never block the
    # durable enqueue below, which is the part that actually matters.
    try:
        await asyncio.to_thread(github_app.react_eyes_to_pr, repo_full_name, pr_number)
    except Exception:  # noqa: BLE001
        logger.exception("failed to react to %s#%s", repo_full_name, pr_number)

    head_sha = (pull_request.get("head") or {}).get("sha")
    logger.info(
        "Enqueuing review ticket for %s#%s (head_sha=%s, provider=%s)",
        repo_full_name,
        pr_number,
        head_sha,
        active_provider(),
    )
    await asyncio.to_thread(
        store.enqueue_or_update,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        provider=active_provider(),
        now=datetime.now(timezone.utc).isoformat(),
    )
    logger.info("Enqueued review ticket for %s#%s", repo_full_name, pr_number)


@router.post("/webhook")
async def webhook(request: Request) -> Response:
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery")
    event_type = request.headers.get("X-GitHub-Event")
    logger.info(
        "Received webhook: event=%s delivery=%s bytes=%d",
        event_type,
        delivery_id,
        len(raw_body),
    )

    if not verify_signature(raw_body, signature_header, settings.github_webhook_secret):
        logger.warning("Rejected webhook: invalid signature (delivery=%s)", delivery_id)
        return Response(status_code=401)

    if delivery_id is not None and _is_duplicate_delivery(delivery_id):
        logger.info("Duplicate delivery=%s; already processed", delivery_id)
        return Response(status_code=200, content="already processed")

    payload = json.loads(raw_body)
    await _handle_pull_request_payload(payload)
    if delivery_id is not None:
        _mark_delivery_processed(delivery_id)
    return Response(status_code=202)
