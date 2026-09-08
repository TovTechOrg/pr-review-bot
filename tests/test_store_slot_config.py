"""review_queue/store.py's slot_config CRUD: durable per (provider, slot_index),
independent of which slot is active (runtime_config's *_key_index columns) --
see docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 4a."""
from __future__ import annotations

import pytest

from review_queue import store

pytestmark = pytest.mark.usefixtures("db")


def test_get_slot_config_returns_none_for_unconfigured_slot():
    assert store.get_slot_config("groq", 3) is None


def test_set_then_get_round_trips_all_three_fields():
    store.set_slot_config(
        "vertex", 1,
        model="gemini-2.5-flash",
        vertex_gcp_project="proj-a",
        vertex_gcp_location="us-east1",
        now="2026-09-08T00:00:00+00:00",
    )
    row = store.get_slot_config("vertex", 1)
    assert row == {
        "model": "gemini-2.5-flash",
        "vertex_gcp_project": "proj-a",
        "vertex_gcp_location": "us-east1",
    }


def test_gemini_groq_rows_leave_vertex_only_fields_null():
    store.set_slot_config(
        "groq", 0, model="llama-3.3-70b-versatile",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    row = store.get_slot_config("groq", 0)
    assert row["model"] == "llama-3.3-70b-versatile"
    assert row["vertex_gcp_project"] is None
    assert row["vertex_gcp_location"] is None


def test_switching_active_slot_never_touches_other_slots_config():
    """The persistence guarantee from spec section 4a: configuring slot 2,
    then setting slot 0 as active elsewhere (runtime_config, not touched by
    this module at all), must never affect slot 2's stored row."""
    store.set_slot_config(
        "gemini", 2, model="gemini-2.0-flash-exp",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    store.set_key_index_override("gemini", 0, now="2026-09-08T00:00:01+00:00")
    row = store.get_slot_config("gemini", 2)
    assert row["model"] == "gemini-2.0-flash-exp"


def test_delete_removes_the_row_entirely():
    store.set_slot_config(
        "groq", 1, model="gemma2-9b-it",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    store.delete_slot_config("groq", 1)
    assert store.get_slot_config("groq", 1) is None


def test_get_all_slot_configs_returns_every_configured_row_keyed_by_provider_and_slot():
    store.set_slot_config(
        "groq", 0, model="llama-3.3-70b-versatile",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    store.set_slot_config(
        "vertex", 1, model="gemini-2.5-flash",
        vertex_gcp_project="proj-a", vertex_gcp_location="us-east1",
        now="2026-09-08T00:00:00+00:00",
    )
    all_configs = store.get_all_slot_configs()
    assert all_configs[("groq", 0)]["model"] == "llama-3.3-70b-versatile"
    assert all_configs[("vertex", 1)]["vertex_gcp_project"] == "proj-a"
