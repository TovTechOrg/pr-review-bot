"""The provider actually in force: read from the DB-refresh cache, no env
fallback (see docs/superpowers/specs/2026-09-09-provider-key-index-db-only-
design.md). Generalizes the 2026-09-08 slotted-config work's DB-only
treatment (already applied to model) to provider selection itself.

Every read of the active provider goes through active_provider(). Partial
adoption would be a bug -- if only the dispatcher consulted the cache, the
factory would still build whatever the cache was empty for, gating on one
provider while calling another.

This module deliberately imports nothing DB-related: the DB read lives in
the dispatcher (where the asyncio.to_thread convention applies) and is
pushed in via set_override_cache. That keeps webhook.py from pulling the DB
driver in through this import, and keeps active_provider() non-blocking.

An empty cache (before the first refresh, or the DB row is NULL) returns ""
-- a real "unconfigured" state, not a default to silently run. The caller
(main.py's lifespan) is responsible for turning that into a boot failure.
"""

from __future__ import annotations

_override: str = ""


def active_provider() -> str:
    return _override


def set_override_cache(value: str | None) -> None:
    global _override
    _override = value or ""


def reset_override_cache() -> None:
    set_override_cache("")
