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

_base: float | None = None
_cap: float | None = None
_factor: float | None = None


def effective_config() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) as cached -- None values mean "not yet refreshed
    this process", not "use a default": DB is the sole source of truth, per
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
    section 10.4. An override that reads back invalid (factor < 1, base >
    cap, non-positive base/cap) is discarded as a whole triple -- (None,
    None, None) -- same as an unrefreshed cache, so a caller can't tell "bad
    data" from "not refreshed yet" and must treat both the same way (defer
    the ticket, don't guess). A base of exactly 0 is valid (immediate
    re-review, no wait) -- only a negative base is rejected; this differs
    from the plan/spec's literal `base <= 0` snippet, which was copied from
    the pre-refactor fallback branch where it was a no-op (that branch
    always returned the same settings-derived base regardless), not an
    intentional "0 is invalid" design decision."""
    if _base is None or _cap is None or _factor is None:
        return (None, None, None)
    if _factor < 1.0 or _base > _cap or _base < 0 or _cap <= 0:
        return (None, None, None)
    return (_base, _cap, _factor)


def set_override_cache(base: float | None, cap: float | None, factor: float | None) -> None:
    global _base, _cap, _factor
    _base, _cap, _factor = base, cap, factor


def reset_override_cache() -> None:
    set_override_cache(None, None, None)
