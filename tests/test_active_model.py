"""The model actually in force per provider: a DB override when set, else the
env value from registry.PROVIDERS. Mirrors tests/test_key_index_override.py."""
from __future__ import annotations

import pytest

from config import settings
from providers import active_model


@pytest.fixture(autouse=True)
def _clean_cache():
    active_model.reset_override_cache()
    yield
    active_model.reset_override_cache()


def test_falls_back_to_the_env_model_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "vertex_model", "env-vertex")
    assert active_model.active_model("vertex") == "env-vertex"


def test_override_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "vertex_model", "env-vertex")
    active_model.set_override_cache({"vertex": "override-vertex"})
    assert active_model.active_model("vertex") == "override-vertex"


def test_each_provider_tracks_its_own_model(monkeypatch):
    """A provider flip must not drag another provider's model with it."""
    monkeypatch.setattr(settings, "groq_model", "env-groq")
    active_model.set_override_cache({"vertex": "override-vertex"})
    assert active_model.active_model("groq") == "env-groq"


def test_empty_override_degrades_to_env(monkeypatch):
    """Fail-safe: a blank hand-edited row must not blank out the model."""
    monkeypatch.setattr(settings, "groq_model", "env-groq")
    active_model.set_override_cache({"groq": ""})
    assert active_model.active_model("groq") == "env-groq"


def test_unknown_provider_degrades_to_the_gemini_model(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "env-gemini")
    assert active_model.active_model("nonesuch") == "env-gemini"


def test_empty_env_model_degrades_to_the_gemini_model(monkeypatch):
    """Unreachable today since every registry model var maps to a real,
    non-empty-by-default Settings field -- but if one were ever hand-set to
    empty (e.g. VERTEX_MODEL="" in .env.config), this value goes straight to
    a live provider SDK, not just a display string, so it must never come
    back as "" the way an unset/empty DB override is allowed to degrade."""
    monkeypatch.setattr(settings, "vertex_model", "")
    monkeypatch.setattr(settings, "gemini_model", "env-gemini")
    assert active_model.active_model("vertex") == "env-gemini"
