# providers/active_vertex_slot.py
"""Vertex's project/location actually in force per credential slot -- DB
override only, no env fallback. Mirrors providers/active_model.py exactly;
see that module's docstring and docs/superpowers/specs/2026-09-08-slotted-
config-and-db-delegation-design.md sections 4b/10.5."""

from __future__ import annotations

_overrides: dict[int, tuple[str | None, str | None]] = {}


def active_vertex_project(index: int) -> str | None:
    project, _ = _overrides.get(index, (None, None))
    return project if project else None


def active_vertex_location(index: int) -> str | None:
    _, location = _overrides.get(index, (None, None))
    return location if location else None


def set_override_cache(overrides: dict[int, tuple[str | None, str | None]]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
