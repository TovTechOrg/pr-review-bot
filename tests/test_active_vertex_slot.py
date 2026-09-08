from __future__ import annotations

from providers import active_vertex_slot


def test_returns_none_for_unconfigured_slot():
    active_vertex_slot.reset_override_cache()
    assert active_vertex_slot.active_vertex_project(0) is None
    assert active_vertex_slot.active_vertex_location(0) is None


def test_returns_the_configured_slot_pair():
    active_vertex_slot.set_override_cache({1: ("proj-a", "us-east1")})
    assert active_vertex_slot.active_vertex_project(1) == "proj-a"
    assert active_vertex_slot.active_vertex_location(1) == "us-east1"
    active_vertex_slot.reset_override_cache()
