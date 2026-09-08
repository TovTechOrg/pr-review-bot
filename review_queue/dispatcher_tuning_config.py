# review_queue/dispatcher_tuning_config.py
"""The 9 dispatcher/timeout tuning knobs actually in force -- DB-only, no
env fallback. Mirrors review_queue/cooldown_config.py's cache-refresh shape
exactly, minus the fallback branch: see docs/superpowers/specs/2026-09-08-
slotted-config-and-db-delegation-design.md section 10.4 for why the
fallback was removed (the singleton row is guaranteed seeded by
store._seed_runtime_config_defaults on first boot, so "missing" no longer
needs a graceful degrade -- it would only ever mean a genuine setup bug,
which should be visible, not papered over).

Every read goes through effective_config(). The DB read lives in the
dispatcher (asyncio.to_thread convention); pushed in via set_override_cache,
keeping this module import-light and non-blocking.

An empty cache (before the first refresh, or after a failed one) reads back
as {} -- callers must treat that as "config not yet available this tick",
not synthesize a default for it. In practice the dispatcher's own refresh
call (review_queue/dispatcher.py::process_next_due) runs before any of
these values are consulted, so {} is only ever transiently observable in a
test that calls effective_config() without first calling set_override_cache.
"""

from __future__ import annotations

_config: dict = {}


def effective_config() -> dict:
    return dict(_config)


def set_override_cache(config: dict) -> None:
    global _config
    _config = config


def reset_override_cache() -> None:
    set_override_cache({})
