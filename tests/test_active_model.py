"""The model actually in force per (provider, credential slot): a DB
override when set for that exact slot, else None -- no env fallback (see
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
sections 4b/10.5). Mirrors tests/test_key_index_override.py."""
from __future__ import annotations

import pytest

from providers import active_model


@pytest.fixture(autouse=True)
def _clean_cache():
    active_model.reset_override_cache()
    yield
    active_model.reset_override_cache()


def test_active_model_is_keyed_by_provider_and_slot():
    active_model.set_override_cache(
        {("groq", 0): "llama-3.3-70b-versatile", ("groq", 1): "gemma2-9b-it"}
    )
    assert active_model.active_model("groq", 0) == "llama-3.3-70b-versatile"
    assert active_model.active_model("groq", 1) == "gemma2-9b-it"


def test_active_model_returns_none_for_unconfigured_slot():
    assert active_model.active_model("gemini", 0) is None


def test_each_slot_tracks_its_own_model():
    """A slot flip must not drag another slot's model with it."""
    active_model.set_override_cache({("vertex", 0): "override-vertex"})
    assert active_model.active_model("groq", 0) is None


def test_empty_override_is_treated_as_unconfigured():
    """Fail-safe: a blank hand-edited row must not read back as a real
    model name."""
    active_model.set_override_cache({("groq", 0): ""})
    assert active_model.active_model("groq", 0) is None
