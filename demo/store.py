"""In-memory stand-in for review_queue.store.

Deferral and cooldown functions are stubs: the mock provider never returns 429
and never fails, so those paths are unreachable in the demo. Simulating
Postgres deferral semantics for a path a demo cannot reach is not worth the
maintenance. tests/test_demo_store.py::test_mock_covers_the_real_store_public_surface
fails if the real store grows a function this module lacks.
"""

from __future__ import annotations

import inspect
import itertools

from datetime import datetime, time

from review_queue import store as real_store
from review_queue.store import Ticket

_ACTIVE_PROVIDER = "groq"
_ids = itertools.count(1)
_tickets: dict[int, Ticket] = {}
_reviews: list[dict] = []

_RUNTIME_CONFIG = {
    "cooldown_base_seconds": 60.0,
    "cooldown_max_seconds": 3600.0,
    "cooldown_factor": 2.0,
    "key_usage_token_cap": 1_000_000,
    "key_usage_reset_time_utc": "00:00",
}
_TUNING = {
    # The 9 knobs review_queue/dispatcher_tuning_config.py's problems()
    # validates on every boot (main.py's lifespan gate) -- see that module's
    # _BOUNDS for the exact predicates these values must satisfy -- plus
    # dispatcher_idle_sleep_seconds, which get_dispatcher_tuning_config()
    # includes for parity with the real store's 10-key shape (the dashboard
    # config panel's one editable surface) even though it isn't itself
    # validated by problems().
    "llm_request_timeout_seconds": 30.0,
    "dispatcher_default_retry_after_seconds": 1.0,
    "dispatcher_failure_base_backoff_seconds": 1.0,
    "dispatcher_failure_max_backoff_seconds": 60.0,
    "dispatcher_max_failure_attempts": 5,
    "dispatcher_max_notice_post_attempts": 3,
    "dispatcher_min_retry_after_seconds": 0.0,
    "dispatcher_backoff_jitter_seconds": 0.0,
    "dispatcher_notice_sweep_batch_size": 50,
    "dispatcher_idle_sleep_seconds": 1.0,
}


def reset() -> None:
    """Demo-only: clear all in-memory state (used by tests and TTL sweeps)."""
    global _tickets, _reviews
    _tickets = {}
    _reviews = []


#  Delegate wrappers below (effective_cooldown/next_cooldown_level/
# usage_bucket_start) call through to `real_store.<name>` by design -- see
# their own docstrings. install() must never rebind those specific names:
# doing so would point real_store.<name> at this module's wrapper, and the
# wrapper's own body calls real_store.<name>, which by then *is* the
# wrapper -- infinite recursion. Excluding them is safe with no loss of
# behavior, since they're pure passthroughs to logic install() never
# touches anyway.
_NEVER_REBIND = frozenset({"effective_cooldown", "next_cooldown_level", "usage_bucket_start"})


def install() -> None:
    """Demo-only: rebind every public review_queue.store function to this
    module's in-memory implementation, in place, on the real module object.

    Every part of the codebase imports the store the same way
    (``from review_queue import store`` and calls ``store.foo(...)``), so
    patching attributes directly on ``review_queue.store`` -- rather than
    swapping a ``sys.modules`` entry, which only affects imports that happen
    *after* the swap -- reaches every caller regardless of import order.
    """
    for name in _public_names() - _NEVER_REBIND:
        setattr(real_store, name, globals()[name])


def _public_names() -> set[str]:
    return {
        name for name, obj in vars(real_store).items()
        if not name.startswith("_") and inspect.isfunction(obj)
        and obj.__module__ == real_store.__name__
    }


def init_pool() -> None:
    return None


def close_pool() -> None:
    return None


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Mirrors the real signature exactly: keyword-only, returns the new id.

    One ticket per (repo, pr) like the real ON CONFLICT clause, so a reader
    refreshing does not pile up rows.
    """
    for existing in _tickets.values():
        if existing.repo_full_name == repo_full_name and existing.pr_number == pr_number:
            existing.head_sha = head_sha
            existing.status = "pending"
            existing.updated_at = now
            return existing.id

    ticket_id = next(_ids)
    # Ticket is a plain dataclass with 17 required fields and no defaults --
    # every one must be supplied or this raises TypeError.
    _tickets[ticket_id] = Ticket(
        id=ticket_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        status="pending",
        provider=provider,
        not_before=None,
        attempts=0,
        comment_id=None,
        enqueued_at=now,
        updated_at=now,
        rereview_requested=0,
        last_reviewed_at=None,
        cooldown_level=0,
        notice_not_before=None,
        last_error=None,
        defer_reason=None,
    )
    return ticket_id


def claim_next_due(now: str) -> Ticket | None:
    for ticket in _tickets.values():
        if ticket.status == "pending":
            ticket.status = "running"
            ticket.attempts += 1
            ticket.updated_at = now
            return ticket
    return None


def get_provider_override() -> str | None:
    return _ACTIVE_PROVIDER


def set_provider_override(provider: str | None, now: str) -> None:
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider or "groq"


def get_key_index_override(provider: str) -> int | None:
    return 0


def set_key_index_override(provider: str, index: int | None, now: str) -> None:
    return None


def get_all_key_index_overrides() -> dict[str, int]:
    return {_ACTIVE_PROVIDER: 0}


def get_slot_config(provider: str, slot_index: int) -> dict | None:
    from demo.provider import demo_provider_and_model
    _, model = demo_provider_and_model(provider)
    return {"provider": provider, "slot_index": slot_index, "model": model}


def set_slot_config(
    provider: str,
    slot_index: int,
    *,
    model: str | None,
    vertex_gcp_project: str | None,
    vertex_gcp_location: str | None,
    now: str,
) -> None:
    return None


def delete_slot_config(provider: str, slot_index: int) -> None:
    return None


def get_all_slot_configs() -> dict[tuple[str, int], dict]:
    """Only the currently active provider's slot 0 is ever "configured" in
    the demo -- MockProvider never reads a real credential, but
    orchestrator.py's own `_active_model()` still resolves the model to
    report/price purely through this cache (`providers/active_model.py`,
    populated once per claimed ticket by
    review_queue/dispatcher.py::_refresh_slot_config), independently of
    specialists.base.get_provider()'s demo patch. Returning {} unconditionally
    (as this did before) left that cache permanently empty, so
    `_active_model()` always raised "no model configured" and no demo review
    could ever reach a completed/finalized ticket -- caught by
    tests/test_demo_app_boot.py's new end-to-end dispatcher test."""
    from demo.provider import demo_provider_and_model
    _, model = demo_provider_and_model(_ACTIVE_PROVIDER)
    return {
        (_ACTIVE_PROVIDER, 0): {
            "model": model,
            "vertex_gcp_project": None,
            "vertex_gcp_location": None,
        }
    }


def get_cooldown_overrides() -> tuple[float | None, float | None, float | None]:
    return (
        _RUNTIME_CONFIG["cooldown_base_seconds"],
        _RUNTIME_CONFIG["cooldown_max_seconds"],
        _RUNTIME_CONFIG["cooldown_factor"],
    )


def set_cooldown_override(
    base: float | None, cap: float | None, factor: float | None, now: str
) -> None:
    return None


def get_usage_cap_overrides() -> tuple[int | None, str | None]:
    return _RUNTIME_CONFIG["key_usage_token_cap"], _RUNTIME_CONFIG["key_usage_reset_time_utc"]


def set_usage_cap_override(tokens: int | None, reset: str | None, now: str) -> None:
    return None


def get_dispatcher_tuning_config() -> dict:
    return dict(_TUNING)


def set_dispatcher_tuning_config(
    *,
    llm_request_timeout_seconds: float | None,
    dispatcher_default_retry_after_seconds: float | None,
    dispatcher_failure_base_backoff_seconds: float | None,
    dispatcher_failure_max_backoff_seconds: float | None,
    dispatcher_max_failure_attempts: int | None,
    dispatcher_max_notice_post_attempts: int | None,
    dispatcher_min_retry_after_seconds: float | None,
    dispatcher_backoff_jitter_seconds: float | None,
    dispatcher_notice_sweep_batch_size: int | None,
    dispatcher_idle_sleep_seconds: float | None,
    now: str,
) -> None:
    return None


def get_idle_sleep_seconds() -> float | None:
    return _TUNING["dispatcher_idle_sleep_seconds"]


def get_review_draft_override() -> bool | None:
    return False


def set_review_draft_override(value: bool | None, now: str) -> None:
    return None


def recover_on_startup(now: str) -> None:
    return None


def record_review(
    repo_full_name: str,
    pr_number: int,
    review,  # specialists.schemas.ReviewResult
    comment_id: int | None,
    now: str,
    key_index: int,
) -> None:
    """Positional, exactly like the real one, and stores the SQL-aliased shape
    `dashboard_reviews()` returns -- not the argument names."""
    from demo.session import current_session

    row = {
        "repo": repo_full_name,
        "pr_number": pr_number,
        "provider": review.provider,
        "model": review.model,
        "created_at": now,
        "elapsed_ms": review.total_elapsed_ms,
        "tokens_in": review.total_tokens_in,
        "tokens_out": review.total_tokens_out,
        "est_cost_usd": review.est_cost_usd,
        "comment_url": (
            f"https://github.com/{repo_full_name}/pull/{pr_number}"
            f"#issuecomment-{comment_id}"
            if comment_id is not None
            else None
        ),
        "specialists": [r.model_dump() for r in review.results],
    }
    row["_session"] = current_session.get()
    _reviews.append(row)


def dashboard_reviews(limit: int = 50) -> list[dict]:
    """Read-time filtered so one reader never sees another's run.

    A cookie-less visitor (session None) sees every unowned review, which is
    the stateless shared view the spec calls for.
    """
    from demo.session import current_session

    session = current_session.get()
    visible = [
        review for review in reversed(_reviews)
        if review.get("_session") in (session, None)
    ]
    return [{k: v for k, v in r.items() if k != "_session"} for r in visible][:limit]


_TICKET_STATUSES = (
    "pending", "running", "deferred", "retrying", "done", "failed", "cancelled"
)


def dashboard_stats() -> dict:
    """Mirrors the real store's SQL aggregate (COUNT/SUM/AVG with COALESCE
    for the zero-reviews case) over the in-memory `_reviews` list."""
    n = len(_reviews)
    if n == 0:
        return {"total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0}
    total_cost = sum(r["est_cost_usd"] for r in _reviews)
    avg_elapsed = sum(r["elapsed_ms"] for r in _reviews) / n
    return {
        "total_reviews": n,
        "total_cost_usd": round(float(total_cost), 4),
        "avg_elapsed_ms": int(avg_elapsed),
    }


def dashboard_queue_counts() -> dict[str, int]:
    counts = {status: 0 for status in _TICKET_STATUSES}
    for ticket in _tickets.values():
        counts[ticket.status] = counts.get(ticket.status, 0) + 1
    return counts


def dashboard_failed_tickets(limit: int = 100) -> list[dict]:
    return []


def finalize_review(
    ticket_id: int,
    now: str,
    rereview_not_before: str,
    rereview_cooldown_level: int,
    comment_id: int | None = None,
) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is None:
        return
    ticket.status = "done"
    ticket.last_reviewed_at = now
    ticket.updated_at = now
    if comment_id is not None:
        ticket.comment_id = comment_id


def set_comment_id(ticket_id: int, comment_id: int | None) -> None:
    if ticket_id in _tickets:
        _tickets[ticket_id].comment_id = comment_id


def get_ticket(ticket_id: int) -> Ticket | None:
    return _tickets.get(ticket_id)


def tickets_needing_notice(now: str, limit: int) -> list[Ticket]:
    return []


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    return None


def clear_notice(ticket_id: int) -> None:
    return None


def clear_visible_review(ticket_id: int) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is not None:
        ticket.last_reviewed_at = None


def cancel_ticket(*, repo_full_name: str, pr_number: int, now: str) -> Ticket | None:
    for ticket in _tickets.values():
        if (
            ticket.repo_full_name == repo_full_name
            and ticket.pr_number == pr_number
            and ticket.status in ("pending", "deferred", "retrying")
        ):
            ticket.status = "cancelled"
            ticket.updated_at = now
            return ticket
    return None


def discard_skipped_ticket(ticket_id: int, now: str) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is None:
        return
    if ticket.rereview_requested:
        ticket.status = "pending"
        ticket.not_before = None
        ticket.attempts = 0
        ticket.rereview_requested = 0
        ticket.defer_reason = None
        ticket.updated_at = now
    elif ticket.last_reviewed_at is None:
        del _tickets[ticket_id]
    else:
        ticket.status = "done"
        ticket.not_before = None
        ticket.rereview_requested = 0
        ticket.defer_reason = None
        ticket.updated_at = now


def migrate_repo_rename(old_full_name: str, new_full_name: str, now: str) -> None:
    return None


def mark_failed(ticket_id: int, now: str, error: str | None = None) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is not None:
        ticket.status = "failed"
        ticket.last_error = error
        ticket.updated_at = now


def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is not None:
        ticket.status = "deferred"
        ticket.not_before = not_before
        ticket.defer_reason = None
        ticket.updated_at = now


def defer_usage_capped(ticket_id: int, not_before: str, now: str) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is not None:
        ticket.status = "deferred"
        ticket.not_before = not_before
        ticket.defer_reason = "usage_cap"
        ticket.updated_at = now


def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    ticket = _tickets.get(ticket_id)
    if ticket is not None:
        ticket.status = "retrying"
        ticket.not_before = not_before
        ticket.attempts += 1
        ticket.updated_at = now


def get_key_usage(provider: str, key_index: int, since: str) -> int:
    return 0


# Thin delegates, not reimplementations: the parity test in
# tests/test_demo_store.py only credits a name as covered when it is a
# function *defined in this module* (``obj.__module__ == module.__name__``),
# so a plain `from review_queue.store import effective_cooldown` -- which
# leaves `effective_cooldown.__module__ == "review_queue.store"` -- would
# still show up as "missing" here. These wrappers exist only to satisfy that
# check; the real logic lives in review_queue.store and is never duplicated.
def effective_cooldown(level: int) -> float:
    return real_store.effective_cooldown(level)


def next_cooldown_level(level: int) -> int:
    return real_store.next_cooldown_level(level)


def usage_bucket_start(now: datetime, reset_time: time) -> datetime:
    return real_store.usage_bucket_start(now, reset_time)
