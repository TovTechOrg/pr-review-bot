"""The per-key daily usage cap actually in force: a DB override (token cap /
reset time) as cached.

Every read of the effective cap goes through effective_caps(). Mirrors
review_queue/cooldown_config.py exactly, including the reason for the split: the
DB read lives in the dispatcher (where the asyncio.to_thread convention
applies) and is pushed in via set_override_cache, keeping this module
import-light and non-blocking.

No env fallback: DB is the sole source of truth, per
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 10.4. The cache starts empty, so before the first refresh -- and
whenever a refresh fails -- effective_caps() returns (None, None), which a
caller must treat as "config not yet available this tick" (defer), not
synthesize a default for. An override that reads back invalid (a reset time
that will not parse) is discarded as a WHOLE PAIR -- (None, None) -- never
partially applied, so a bad field can never pair with a stale override in
the other field.

Why a non-positive cap is treated as "cap disabled" rather than "invalid":
the dispatcher's gate is `tokens >= cap`, which a 0 cap makes unconditionally
true -- every ticket deferred forever. Rather than reject the override
outright (which would need a fallback to reject TO), a non-positive cap is
normalized to None -- explicitly no cap -- the same value an operator would
use to turn the cap off.
"""

from __future__ import annotations

from datetime import time

_tokens: int | None = None
_reset: str | None = None


def effective_caps() -> tuple[int | None, time | None]:
    """(token cap, reset time) as cached. A cap of None (with a real reset
    time present) means the cap is intentionally disabled -- that's a valid
    configured state, not "unset"; a reset time of None means genuinely not
    yet refreshed/configured, since a cap being off never implies the reset
    time is meaningless (it still gates when a *future* cap would reset)."""
    if _reset is None:
        return (None, None)
    try:
        reset = time.fromisoformat(_reset)
    except ValueError:
        return (None, None)
    if _tokens is not None and _tokens <= 0:
        return (None, reset)
    return (_tokens, reset)


def set_override_cache(tokens: int | None, reset: str | None) -> None:
    """`reset` is the raw "HH:MM"/"HH:MM:SS" text as stored; parsing (and
    rejecting garbage) happens in effective_caps, so a malformed value degrades
    the whole pair at read time rather than raising inside a refresh."""
    global _tokens, _reset
    _tokens, _reset = tokens, reset


def reset_override_cache() -> None:
    set_override_cache(None, None)
