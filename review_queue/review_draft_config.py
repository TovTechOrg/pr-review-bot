"""Whether draft PRs get reviewed: a DB override as cached, no env fallback.

Every read of the effective flag goes through effective_review_draft_prs().
Mirrors review_queue/cooldown_config.py / usage_cap_config.py exactly, including
the reason for the split: the DB read lives in the dispatcher (where the
asyncio.to_thread convention applies) and is pushed in via set_override_cache,
keeping this module import-light and non-blocking.

No env fallback: DB is the sole source of truth, per
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 10.4. The cache starts empty (None), so before the first refresh --
and whenever a refresh fails -- effective_review_draft_prs() returns None,
which a caller must treat as "config not available this tick" (defer/skip
the decision), NOT "treat drafts like non-drafts" as it did before this
change.
"""

from __future__ import annotations

_override: bool | None = None


def effective_review_draft_prs() -> bool | None:
    """The cached override, or None if never refreshed. Unlike the old
    behavior, None is NOT "treat drafts like non-drafts" -- callers must
    treat None as "config not available this tick" (defer/skip the
    decision), matching the no-fallback policy everywhere else in this
    plan."""
    return _override


def set_override_cache(value: bool | None) -> None:
    global _override
    _override = value


def reset_override_cache() -> None:
    set_override_cache(None)
