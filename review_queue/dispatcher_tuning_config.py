# review_queue/dispatcher_tuning_config.py
"""The 9 dispatcher/timeout tuning knobs actually in force -- DB-only, no
env fallback. Mirrors review_queue/cooldown_config.py's cache-refresh shape
exactly, minus the fallback branch: see docs/superpowers/specs/2026-09-08-
slotted-config-and-db-delegation-design.md section 10.4 for why the
fallback was removed (a missing/incomplete row means a genuine setup bug,
which should be visible, not papered over -- store.py no longer seeds any
default values into runtime_config at boot; main.py's lifespan instead
refuses to start at all unless the row is already complete, whoever wrote
it -- see store.init_pool()'s docstring).

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

from collections.abc import Callable

_config: dict = {}


def effective_config() -> dict:
    return dict(_config)


def set_override_cache(config: dict) -> None:
    global _config
    _config = config


def reset_override_cache() -> None:
    set_override_cache({})


class TuningConfigUnavailable(RuntimeError):
    """The 9 knobs are not usably configured this tick: the cache is empty
    (no refresh yet, or the last one failed), a column is NULL, or a value
    is out of the range that made it safe as a Settings field. Distinct from
    a DB-read failure -- see review_queue/dispatcher.py's guard, which turns
    this into a visible ticket deferral rather than an exception escaping
    the failure handler's own backoff math.
    """


# (key, predicate, description). Mirrors the bounds config.py's own Field()
# constraints used to enforce, which stopped applying the moment Settings
# stopped being this value's runtime source -- see
# docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
# section 10.4.
_BOUNDS: tuple[tuple[str, Callable[[float], bool], str], ...] = (
    ("llm_request_timeout_seconds", lambda v: v > 0, "must be > 0"),
    ("dispatcher_default_retry_after_seconds", lambda v: v >= 0, "must be >= 0"),
    ("dispatcher_failure_base_backoff_seconds", lambda v: v >= 0, "must be >= 0"),
    ("dispatcher_failure_max_backoff_seconds", lambda v: v > 0, "must be > 0"),
    ("dispatcher_max_failure_attempts", lambda v: v >= 1, "must be >= 1"),
    ("dispatcher_max_notice_post_attempts", lambda v: v >= 1, "must be >= 1"),
    ("dispatcher_min_retry_after_seconds", lambda v: v >= 0, "must be >= 0"),
    ("dispatcher_backoff_jitter_seconds", lambda v: v >= 0, "must be >= 0"),
    # gt=0 in config.py: 0 disables the sweep, and a NULL column makes
    # store.tickets_needing_notice's LIMIT unbounded -- the exact pre-fix
    # behavior that setting exists to prevent.
    ("dispatcher_notice_sweep_batch_size", lambda v: v >= 1, "must be >= 1"),
)

REQUIRED_KEYS = tuple(key for key, _predicate, _description in _BOUNDS)


def problems(config: dict) -> list[str]:
    """Every reason `config` is unusable, as human-readable strings. Shared
    by require_config() below and scripts/deploy.py's --sync-config-db
    guard, so the CLI and the dispatcher can never disagree about what
    counts as a valid tuning config."""
    found = []
    for key, predicate, description in _BOUNDS:
        if key not in config or config[key] is None:
            found.append(f"{key} is not set")
        elif not predicate(config[key]):
            found.append(f"{key}={config[key]!r} {description}")
    base = config.get("dispatcher_failure_base_backoff_seconds")
    cap = config.get("dispatcher_failure_max_backoff_seconds")
    if base is not None and cap is not None and base > cap:
        found.append(f"base backoff {base} exceeds max backoff {cap}")
    return found


def require_config() -> dict:
    """The 9 knobs, guaranteed present and in-range, or raise. Every
    dispatcher/orchestrator read goes through this rather than
    effective_config() -- there is no env fallback, so an absent or
    out-of-range value is a real failure that must surface as a deferral,
    not a KeyError/TypeError deep in the call stack."""
    config = effective_config()
    found = problems(config)
    if found:
        raise TuningConfigUnavailable("; ".join(found))
    return config
