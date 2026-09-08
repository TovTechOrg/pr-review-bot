"""providers/vertex_credentials.py -- Vertex's two-layer credential
chain: VERTEX_GCP_SERVICE_ACCOUNT_KEY (index-aware) -> None, meaning "let
google-auth discover implicit ADC".

Hermetic by construction: the autouse fixture points the credential at
nothing, because a developer's real .env legitimately sets
VERTEX_GCP_SERVICE_ACCOUNT_KEY to a real value -- without it, the "nothing
resolves" tests would pass in CI and fail locally.
"""
from __future__ import annotations

import base64
import json

import pytest

from config import settings
from providers import vertex_credentials

KEY = {
    "type": "service_account",
    "project_id": "proj-from-key",
    "client_email": "svc@proj-from-key.iam.gserviceaccount.com",
}
OTHER_KEY = {**KEY, "project_id": "proj-from-slot-1"}


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture(autouse=True)
def _no_real_gcp_credentials(monkeypatch):
    monkeypatch.setattr(settings, "vertex_gcp_service_account_key", "")
    for index in (1, 2):
        monkeypatch.delenv(f"VERTEX_GCP_SERVICE_ACCOUNT_KEY_{index}", raising=False)


def test_index_zero_decodes_the_base_env_var(monkeypatch):
    monkeypatch.setattr(settings, "vertex_gcp_service_account_key", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_a_numbered_index_decodes_its_own_env_var(monkeypatch):
    monkeypatch.setattr(settings, "vertex_gcp_service_account_key", _b64(KEY))
    monkeypatch.setenv("VERTEX_GCP_SERVICE_ACCOUNT_KEY_1", _b64(OTHER_KEY))
    assert vertex_credentials.resolve_service_account_info(1) == OTHER_KEY


def test_returns_none_when_nothing_resolves():
    """NOT an error for vertex -- None means "pass no explicit credentials to
    the client", which is exactly what triggers google-auth's implicit ADC
    discovery. Contrast gemini/groq, where an empty credential always means
    misconfigured."""
    assert vertex_credentials.resolve_service_account_info(0) is None


def test_a_numbered_index_does_not_fall_back_to_index_zero(monkeypatch):
    """An unprovisioned slot must resolve to "nothing here", not silently to
    the base slot -- a swap to an empty index must be visible, not a no-op."""
    monkeypatch.setattr(settings, "vertex_gcp_service_account_key", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(2) is None


def test_malformed_base64_raises_rather_than_falling_through(monkeypatch):
    """A corrupt env var must surface, not quietly degrade to implicit ADC --
    that would run against a different account (or none) than the operator
    intended."""
    monkeypatch.setattr(settings, "vertex_gcp_service_account_key", "!!!not-base64!!!")
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_valid_base64_that_is_not_json_raises(monkeypatch):
    monkeypatch.setattr(
        settings, "vertex_gcp_service_account_key", base64.b64encode(b"nope").decode()
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_decoding_to_a_json_list_not_an_object_raises(monkeypatch):
    """The documented contract is `dict | None` -- a syntactically valid JSON
    value that isn't an object (e.g. a list) must surface as an error, not be
    handed to VertexProvider as if it were a service-account key."""
    monkeypatch.setattr(
        settings,
        "vertex_gcp_service_account_key",
        base64.b64encode(json.dumps([1, 2, 3]).encode()).decode(),
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)
