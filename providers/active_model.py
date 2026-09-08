# providers/active_model.py
"""The model name actually in force per (provider, credential slot): a DB
override when set for that exact slot, else None. No env fallback -- see
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
sections 4b/10.5. A slot with no configured model is a real missing-config
state; the caller (providers/factory.py::_build) is responsible for turning
that into a visible failure. This module stays as dependency-free as
providers/key_index.py, on purpose -- it does no I/O and raises nothing
itself.
"""

from __future__ import annotations

_overrides: dict[tuple[str, int], str] = {}


def active_model(provider: str, index: int) -> str | None:
    """The model configured for this exact (provider, slot), or None if
    that slot has never been configured."""
    value = _overrides.get((provider, index))
    return value if value else None


def set_override_cache(overrides: dict[tuple[str, int], str]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
