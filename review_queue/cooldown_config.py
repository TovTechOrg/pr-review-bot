"""The re-review cooldown parameters actually in force: a DB override
(base/cap/factor) as cached.

Every read of the effective cooldown config goes through effective_config().
Mirrors providers/active.py's provider-override cache exactly, including
the reason for the split: the DB read lives in the dispatcher (where the
asyncio.to_thread convention applies) and is pushed in via set_override_cache,
keeping this module import-light and non-blocking.

No env fallback: DB is the sole source of truth, per
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 10.4. The cache starts empty, so before the first refresh -- and
whenever a refresh fails -- effective_config() returns (None, None, None),
which a caller must treat as "config not yet available this tick" (defer),
not synthesize a default for. An override that reads back invalid
(factor < 1, base > cap, a negative base, or a non-positive cap) is
discarded as a WHOLE triple -- (None, None, None), same as an unrefreshed
cache -- never partially applied, so a bad field can never pair with a
stale override in another field.
"""

from __future__ import annotations

from collections.abc import Callable

_base: float | None = None
_cap: float | None = None
_factor: float | None = None

# (key, predicate, description). The single definition of "this cooldown
# triple is unusable", shared by effective_config() below, scripts/deploy.py's
# --sync-config-db guard, dashboard/environment.py's config PATCH, and
# main.py's boot gate -- so no writer can disagree with the reader about
# what it is allowed to store. Mirrors dispatcher_tuning_config._BOUNDS.
#
# A base of exactly 0 is VALID (immediate re-review, no wait); only a
# negative base is rejected. A non-positive cap is not, since it would make
# every escalated wait collapse to it.
_BOUNDS: tuple[tuple[str, Callable[[float], bool], str], ...] = (
    ("cooldown_base_seconds", lambda v: v >= 0, "must be >= 0"),
    ("cooldown_max_seconds", lambda v: v > 0, "must be > 0"),
    ("cooldown_factor", lambda v: v >= 1.0, "must be >= 1.0"),
)


def problems(config: dict) -> list[str]:
    """Every reason `config` is an unusable cooldown triple, as
    human-readable strings. Empty list means usable."""
    found = []
    for key, predicate, description in _BOUNDS:
        if key not in config or config[key] is None:
            found.append(f"{key} is not set")
        elif not predicate(config[key]):
            found.append(f"{key}={config[key]!r} {description}")
    base = config.get("cooldown_base_seconds")
    cap = config.get("cooldown_max_seconds")
    if base is not None and cap is not None and base > cap:
        found.append(f"cooldown base {base} exceeds cooldown max {cap}")
    return found


def effective_config() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) as cached -- None values mean "not yet refreshed
    this process", not "use a default": DB is the sole source of truth, per
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
    section 10.4. An override that problems() rejects is discarded as a
    WHOLE triple -- (None, None, None), same as an unrefreshed cache -- so a
    caller can't tell "bad data" from "not refreshed yet" and must treat both
    the same way (defer the ticket, don't guess).

    The predicate itself lives in problems() above, not here, so the writers
    that validate before storing and this reader that discards after reading
    can never drift apart (see that function's comment).
    """
    config = {
        "cooldown_base_seconds": _base,
        "cooldown_max_seconds": _cap,
        "cooldown_factor": _factor,
    }
    if problems(config):
        return (None, None, None)
    return (_base, _cap, _factor)


def set_override_cache(base: float | None, cap: float | None, factor: float | None) -> None:
    global _base, _cap, _factor
    _base, _cap, _factor = base, cap, factor


def reset_override_cache() -> None:
    set_override_cache(None, None, None)
