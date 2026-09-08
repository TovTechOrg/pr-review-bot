"""Tests for dashboard/environment.py: the Environment tab's Render env-var
and runtime_config endpoints. Route-level, using the same authenticated
AsyncClient pattern dashboard/tests/test_dashboard_page.py already uses."""

from __future__ import annotations

import io

import jwt
from httpx import ASGITransport, AsyncClient

import github_app
import render_client
from config import settings
from main import app
from providers import catalog, credentials, vertex_credentials
from review_queue import store
from dashboard import auth


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    )


async def _unauthenticated_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_unauthenticated_get_render_env_vars_is_rejected():
    client = await _unauthenticated_client()
    resp = await client.get("/api/environment/render")
    assert resp.status_code == 401


async def test_get_render_env_vars_returns_key_and_value(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "env_vars", lambda service_id: {"FOO": "bar"})
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vars"] == [{"key": "FOO", "value": "bar", "protected": False}]


async def test_get_render_env_vars_marks_protected_keys(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(
        render_client, "env_vars", lambda service_id: {"DATABASE_URL": "postgres://x"}
    )
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.json()["vars"] == [
        {"key": "DATABASE_URL", "value": "postgres://x", "protected": True}
    ]


async def test_patch_render_env_vars_applies_sets_and_fires_one_deploy(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    pushed = []
    monkeypatch.setattr(
        render_client,
        "push_env_var",
        lambda service_id, key, value: pushed.append((key, value)),
    )
    deploys = []
    monkeypatch.setattr(
        render_client,
        "trigger_deploy",
        lambda service_id: deploys.append(service_id) or "dep-1",
    )
    client = await _client()
    resp = await client.patch(
        "/api/environment/render", json={"sets": {"FOO": "bar"}, "deletes": []}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == ["FOO"]
    assert body["failed"] == []
    assert body["deploy_id"] == "dep-1"
    assert pushed == [("FOO", "bar")]
    assert deploys == ["srv-1"]


async def test_patch_render_env_vars_rejects_a_protected_delete_without_touching_render(
    monkeypatch,
):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _boom(service_id, key):
        raise AssertionError("delete_env_var must not be called for a protected key")

    monkeypatch.setattr(render_client, "delete_env_var", _boom)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.patch(
        "/api/environment/render", json={"sets": {}, "deletes": ["DATABASE_URL"]}
    )
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "DATABASE_URL", "error": "protected"}]
    # No successful write happened, so no deploy is triggered.
    assert body["deploy_id"] is None


async def test_patch_render_env_vars_a_protected_delete_does_not_block_other_keys(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    deleted = []
    monkeypatch.setattr(
        render_client,
        "delete_env_var",
        lambda service_id, key: deleted.append(key),
    )
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.patch(
        "/api/environment/render",
        json={"sets": {}, "deletes": ["DATABASE_URL", "SOME_OTHER_KEY"]},
    )
    body = resp.json()
    assert body["applied"] == ["SOME_OTHER_KEY"]
    assert {"key": "DATABASE_URL", "error": "protected"} in body["failed"]
    assert deleted == ["SOME_OTHER_KEY"]


async def test_patch_render_env_vars_stops_at_the_first_render_failure(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _push(service_id, key, value):
        if key == "SECOND":
            raise RuntimeError("boom")

    monkeypatch.setattr(render_client, "push_env_var", _push)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.patch(
        "/api/environment/render",
        json={"sets": {"FIRST": "a", "SECOND": "b", "THIRD": "c"}, "deletes": []},
    )
    body = resp.json()
    assert body["applied"] == ["FIRST"]
    assert body["failed"] == [{"key": "SECOND", "error": "RuntimeError"}]
    assert "THIRD" not in body["applied"]


async def test_get_environment_config_reflects_current_overrides(db):
    store.set_provider_override("groq", "2026-01-01T00:00:00+00:00")
    client = await _client()
    resp = await client.get("/api/environment/config")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "groq"


async def test_patch_environment_config_sets_provider_override(db):
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"provider": "groq"})
    assert resp.status_code == 200
    assert resp.json() == {"applied": ["provider"], "failed": []}
    assert store.get_provider_override() == "groq"


async def test_patch_environment_config_partial_cooldown_merges_with_current_values(db):
    store.set_cooldown_override(1.0, 2.0, 3.0, "2026-01-01T00:00:00+00:00")
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"cooldown_base_seconds": 9.0})
    assert resp.status_code == 200
    assert store.get_cooldown_overrides() == (9.0, 2.0, 3.0)


async def test_patch_environment_config_rejects_an_unknown_provider_in_key_index():
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"key_index": {"not-a-provider": 1}})
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "key_index.not-a-provider", "error": "unknown_provider"}]


async def test_patch_environment_config_rejects_an_unknown_top_level_provider():
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"provider": "not-a-provider"})
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "provider", "error": "unknown_provider"}]


async def test_guided_gemini_validate_success(monkeypatch):
    def _list_models(api_key):
        assert api_key == "the-key"
        return catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None)

    monkeypatch.setattr(catalog, "list_gemini_models", _list_models)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/gemini/validate", data={"api_key": "the-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["models"] == ["gemini-flash-latest"]
    assert body["conflicts"] == []


async def test_guided_gemini_validate_failure_is_structural(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "list_gemini_models",
        lambda api_key: catalog.CatalogResult(ok=False, models=None, error="unauthorized"),
    )
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/gemini/validate", data={"api_key": "bad-key"}
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "unauthorized"


async def test_guided_vertex_validate_uploads_file_and_flags_project_conflict(monkeypatch):
    key_json = b'{"project_id": "new-proj", "token_uri": "https://oauth2.googleapis.com/token"}'

    def _list_vertex(info, project_override=None, location_override=None):
        assert info == {
            "project_id": "new-proj",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        assert project_override == "new-proj"
        return catalog.CatalogResult(ok=True, models=["gemini-2.5-flash"], error=None)

    monkeypatch.setattr(catalog, "list_vertex_models", _list_vertex)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(
        render_client, "env_vars", lambda service_id: {"VERTEX_GCP_PROJECT": "old-proj"}
    )
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/vertex/validate",
        files={"credential_file": ("key.json", io.BytesIO(key_json), "application/json")},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["project_id"] == "new-proj"
    assert body["conflicts"] == [
        {"var": "VERTEX_GCP_PROJECT", "current": "old-proj", "new": "new-proj"}
    ]


async def test_guided_github_app_validate_success_shows_installation_id(monkeypatch):
    monkeypatch.setattr(
        github_app, "_app_jwt_client_for", lambda app_id, private_key_b64: "fake-client"
    )

    def _discover(client):
        assert client == "fake-client"
        return 4242

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _discover)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "123"},
        files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["installation_id"] == 4242


async def test_guided_github_app_validate_no_installation_is_structural_error(monkeypatch):
    monkeypatch.setattr(
        github_app, "_app_jwt_client_for", lambda app_id, private_key_b64: "fake-client"
    )

    def _raise(client):
        raise github_app.AppNotInstalledError("no installation")

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "123"},
        files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
    )
    assert resp.json()["error"] == "installation_not_found"


async def test_guided_gemini_apply_writes_credential_only_to_render(monkeypatch, db):
    """Model lives in slot_config only now -- Render never sees it."""
    applied = {}
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _push(service_id, key, value):
        applied[key] = value

    monkeypatch.setattr(render_client, "push_env_var", _push)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/gemini/apply",
        json={"slot": 0, "credential": {"api_key": "the-key"}, "model": "gemini-flash-latest"},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert "GEMINI_API_KEY" in result["applied"]
    assert applied == {"GEMINI_API_KEY": "the-key"}  # not GEMINI_MODEL


async def test_apply_writes_model_to_slot_config(monkeypatch, db):
    monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    await client.post(
        "/api/environment/credential/groq/apply",
        json={"slot": 2, "credential": {"api_key": "real-key"}, "model": "gemma2-9b-it"},
    )
    row = store.get_slot_config("groq", 2)
    assert row["model"] == "gemma2-9b-it"


async def test_apply_writes_vertex_project_and_location_to_slot_config(monkeypatch, db):
    monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    await client.post(
        "/api/environment/credential/vertex/apply",
        json={
            "slot": 0,
            "credential": {"service_account_b64": "..."},
            "model": "gemini-2.5-flash",
            "vertex_gcp_project": "proj-a",
            "vertex_gcp_location": "us-east1",
        },
    )
    row = store.get_slot_config("vertex", 0)
    assert row == {
        "model": "gemini-2.5-flash",
        "vertex_gcp_project": "proj-a",
        "vertex_gcp_location": "us-east1",
    }


async def test_apply_reports_slot_config_write_failure_as_a_named_failed_entry(monkeypatch, db):
    """Acceptance criterion from docs/superpowers/specs/2026-09-08-slotted-
    config-and-db-delegation-design.md section 8: a partial failure must be
    visible, not silently swallowed."""
    monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _boom(*a, **kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "set_slot_config", _boom)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/groq/apply",
        json={"slot": 0, "credential": {"api_key": "real-key"}, "model": "llama-3.3-70b-versatile"},
    )
    body = resp.json()
    assert "GROQ_API_KEY" in body["applied"]
    assert any(f["key"] == "slot_config.groq.0" for f in body["failed"])


async def test_guided_github_app_apply_writes_id_key_and_installation(monkeypatch):
    applied = {}
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _push(service_id, key, value):
        applied[key] = value

    monkeypatch.setattr(render_client, "push_env_var", _push)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/apply",
        json={"app_id": "123", "private_key_b64": "cGVt", "installation_id": 4242},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert applied["GITHUB_APP_ID"] == "123"
    assert applied["GITHUB_APP_PRIVATE_KEY"] == "cGVt"
    assert applied["GITHUB_APP_INSTALLATION_ID"] == "4242"
    assert set(result["applied"]) == {
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
    }


async def test_render_payload_includes_available_key_slots(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(
        render_client,
        "env_vars",
        lambda service_id: {
            "GEMINI_API_KEY": "x",
            "GEMINI_API_KEY_2": "y",
            "GROQ_API_KEY_1": "z",
            "DATABASE_URL": "postgres://...",
        },
    )
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.status_code == 200
    slots = resp.json()["available_key_slots"]
    assert slots["gemini"] == [0, 2]
    assert slots["groq"] == [1]
    assert slots["vertex"] == []


async def test_render_payload_available_key_slots_empty_when_no_service(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: None)
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.json()["available_key_slots"] == {"gemini": [], "groq": [], "vertex": []}


async def test_credential_models_refresh_resolves_current_slot(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"gemini": 2})

    def _resolve(provider, index):
        assert (provider, index) == ("gemini", 2)
        return ("GEMINI_API_KEY_2", "resolved-key")

    monkeypatch.setattr(credentials, "resolve", _resolve)

    def _list_models(api_key):
        assert api_key == "resolved-key"
        return catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None)

    monkeypatch.setattr(catalog, "list_gemini_models", _list_models)

    client = await _client()
    resp = await client.get("/api/environment/credential/gemini/models")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "models": ["gemini-flash-latest"], "error": None}


async def test_credential_models_refresh_explicit_slot_overrides_current(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"gemini": 0})

    def _resolve(provider, index):
        assert index == 3
        return ("GEMINI_API_KEY_3", "key-3")

    monkeypatch.setattr(credentials, "resolve", _resolve)
    monkeypatch.setattr(
        catalog,
        "list_gemini_models",
        lambda api_key: catalog.CatalogResult(ok=True, models=["m"], error=None),
    )
    client = await _client()
    resp = await client.get("/api/environment/credential/gemini/models?slot=3")
    assert resp.status_code == 200


async def test_credential_models_refresh_no_credential_configured(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(credentials, "resolve", lambda provider, index: ("X", ""))
    client = await _client()
    resp = await client.get("/api/environment/credential/gemini/models")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "models": None, "error": "no_credential_configured"}


async def test_credential_models_refresh_rejects_github_app_family():
    client = await _client()
    resp = await client.get("/api/environment/credential/github_app/models")
    assert resp.status_code == 404


async def test_credential_models_refresh_vertex_resolves_service_account_info(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 0})
    monkeypatch.setattr(
        vertex_credentials, "resolve_service_account_info", lambda index: {"project_id": "proj-a"}
    )

    def _list_vertex(info):
        assert info == {"project_id": "proj-a"}
        return catalog.CatalogResult(ok=True, models=["gemini-2.5-flash"], error=None)

    monkeypatch.setattr(catalog, "list_vertex_models", _list_vertex)
    client = await _client()
    resp = await client.get("/api/environment/credential/vertex/models")
    assert resp.status_code == 200
    assert resp.json()["models"] == ["gemini-2.5-flash"]


async def test_validate_model_var_ok_when_in_catalog(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(credentials, "resolve", lambda provider, index: ("X", "key"))
    monkeypatch.setattr(
        catalog,
        "list_gemini_models",
        lambda api_key: catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None),
    )
    client = await _client()
    resp = await client.post(
        "/api/environment/validate/GEMINI_MODEL", json={"value": "gemini-flash-latest"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None


async def test_validate_model_var_invalid_when_not_in_catalog(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(credentials, "resolve", lambda provider, index: ("X", "key"))
    monkeypatch.setattr(
        catalog,
        "list_gemini_models",
        lambda api_key: catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None),
    )
    client = await _client()
    resp = await client.post(
        "/api/environment/validate/GEMINI_MODEL", json={"value": "not-a-real-model"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "not_in_catalog"


async def test_validate_model_var_no_credential_configured(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(credentials, "resolve", lambda provider, index: ("X", ""))
    client = await _client()
    resp = await client.post(
        "/api/environment/validate/GROQ_MODEL", json={"value": "llama-3.3-70b-versatile"}
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "no_credential_configured"


async def test_validate_gcp_project_substitutes_project_override(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(
        vertex_credentials, "resolve_service_account_info", lambda index: {"project_id": "old-proj"}
    )

    def _list_vertex(info, project_override=None, location_override=None):
        assert project_override == "new-proj"
        return catalog.CatalogResult(ok=True, models=["gemini-2.5-flash"], error=None)

    monkeypatch.setattr(catalog, "list_vertex_models", _list_vertex)
    client = await _client()
    resp = await client.post(
        "/api/environment/validate/VERTEX_GCP_PROJECT", json={"value": "new-proj"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_validate_gcp_location_substitutes_location_override(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(
        vertex_credentials, "resolve_service_account_info", lambda index: {"project_id": "proj-a"}
    )

    def _list_vertex(info, project_override=None, location_override=None):
        assert location_override == "europe-west1"
        return catalog.CatalogResult(ok=True, models=[], error=None)

    monkeypatch.setattr(catalog, "list_vertex_models", _list_vertex)
    client = await _client()
    resp = await client.post(
        "/api/environment/validate/VERTEX_GCP_LOCATION", json={"value": "europe-west1"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_validate_unknown_var_rejected():
    client = await _client()
    resp = await client.post("/api/environment/validate/RANDOM_VAR", json={"value": "x"})
    assert resp.status_code == 404


async def test_patch_render_rejects_model_not_in_catalog_but_applies_other_keys(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "push_env_var", lambda *a, **k: None)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(credentials, "resolve", lambda provider, index: ("X", "key"))
    monkeypatch.setattr(
        catalog,
        "list_gemini_models",
        lambda api_key: catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None),
    )
    client = await _client()
    resp = await client.patch(
        "/api/environment/render",
        json={"sets": {"GEMINI_MODEL": "bogus-model", "OTHER_KEY": "fine"}, "deletes": []},
    )
    result = resp.json()
    assert {"key": "GEMINI_MODEL", "error": "failed_validation"} in result["failed"]
    assert "OTHER_KEY" in result["applied"]


async def test_delete_non_dependent_slot_succeeds_immediately(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "delete_env_var", lambda *a, **k: None)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(store, "get_provider_override", lambda: None)
    monkeypatch.setattr(store, "get_slot_config", lambda family, index: None)
    client = await _client()
    resp = await client.delete("/api/environment/render/GEMINI_API_KEY_2")
    assert resp.status_code == 200
    assert resp.json()["applied"] == ["GEMINI_API_KEY_2"]


async def test_delete_dependent_slot_without_confirm_returns_409(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"gemini": 2})
    monkeypatch.setattr(store, "get_provider_override", lambda: None)
    monkeypatch.setattr(store, "get_slot_config", lambda family, index: None)
    client = await _client()
    resp = await client.delete("/api/environment/render/GEMINI_API_KEY_2")
    assert resp.status_code == 409
    assert resp.json()["dependents"] == ["key_index override"]


async def test_delete_active_provider_credential_without_confirm_returns_409(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(store, "get_provider_override", lambda: "gemini")
    monkeypatch.setattr(store, "get_slot_config", lambda family, index: None)
    client = await _client()
    resp = await client.delete("/api/environment/render/GEMINI_API_KEY")
    assert resp.status_code == 409
    assert resp.json()["dependents"] == ["active provider override"]


async def test_delete_slot_with_configured_slot_config_without_confirm_returns_409(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"groq": 0})
    monkeypatch.setattr(store, "get_provider_override", lambda: None)
    monkeypatch.setattr(
        store,
        "get_slot_config",
        lambda family, index: {
            "model": "gemma2-9b-it",
            "vertex_gcp_project": None,
            "vertex_gcp_location": None,
        },
    )
    client = await _client()
    resp = await client.delete("/api/environment/render/GROQ_API_KEY_3")
    assert resp.status_code == 409
    assert resp.json()["dependents"] == ["slotted model/project/location config"]


async def test_confirmed_delete_of_a_slot_with_slot_config_removes_the_row(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "delete_env_var", lambda *a, **k: None)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"groq": 0})
    monkeypatch.setattr(store, "get_provider_override", lambda: None)
    monkeypatch.setattr(
        store,
        "get_slot_config",
        lambda family, index: {
            "model": "gemma2-9b-it",
            "vertex_gcp_project": None,
            "vertex_gcp_location": None,
        },
    )
    deleted = {}

    def _delete_slot_config(family, index):
        deleted["family"] = family
        deleted["index"] = index

    monkeypatch.setattr(store, "delete_slot_config", _delete_slot_config)
    client = await _client()
    resp = await client.delete("/api/environment/render/GROQ_API_KEY_3?confirm=true")
    assert resp.status_code == 200
    assert resp.json()["applied"] == ["GROQ_API_KEY_3"]
    assert deleted == {"family": "groq", "index": 3}


async def test_confirmed_delete_cascades_runtime_config(monkeypatch):
    cleared = {}
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "delete_env_var", lambda *a, **k: None)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"gemini": 2})
    monkeypatch.setattr(store, "get_provider_override", lambda: "gemini")
    monkeypatch.setattr(store, "get_slot_config", lambda family, index: None)

    def _set_key_index(provider, index, now):
        cleared["key_index"] = (provider, index)

    def _set_provider(provider, now):
        cleared["provider"] = provider

    monkeypatch.setattr(store, "set_key_index_override", _set_key_index)
    monkeypatch.setattr(store, "set_provider_override", _set_provider)
    client = await _client()
    resp = await client.delete("/api/environment/render/GEMINI_API_KEY_2?confirm=true")
    assert resp.status_code == 200
    assert resp.json()["applied"] == ["GEMINI_API_KEY_2"]
    assert cleared["key_index"] == ("gemini", None)
    assert cleared["provider"] is None


async def test_protected_key_delete_still_refused():
    client = await _client()
    resp = await client.delete("/api/environment/render/DATABASE_URL?confirm=true")
    assert resp.status_code == 200
    assert resp.json()["failed"] == [{"key": "DATABASE_URL", "error": "protected"}]


async def test_guided_apply_rejects_malformed_payload_without_crashing():
    """Regression test: a malformed apply body must not 500 or echo the
    submitted credential value back in any way."""
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/gemini/apply",
        json={"slot": 0, "credential": {"api_key": "gsk_supersecrettailvalue"}},
    )
    assert resp.status_code == 422
    assert "gsk_supersecrettailvalue" not in resp.text


async def test_guided_apply_rejects_out_of_range_slot():
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/gemini/apply",
        json={
            "slot": 97,
            "credential": {"api_key": "the-key"},
            "model": "gemini-flash-latest",
        },
    )
    assert resp.status_code == 422


async def test_patch_config_rejects_out_of_range_key_index(db):
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"key_index": {"gemini": 97}})
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "key_index.gemini", "error": "invalid_slot"}]


async def test_guided_github_app_validate_rejects_non_numeric_app_id():
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "not-a-number"},
        files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
    )
    assert resp.status_code == 422


async def test_guided_github_app_validate_empty_file_is_structural_error():
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "123"},
        files={"credential_file": ("app.pem", io.BytesIO(b""), "application/x-pem-file")},
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "invalid_key"


async def test_guided_github_app_validate_malformed_key_is_structural_error(monkeypatch):
    def _raise_jwt_error(app_id, private_key_b64):
        raise jwt.exceptions.InvalidKeyError("could not parse key")

    monkeypatch.setattr(github_app, "_app_jwt_client_for", _raise_jwt_error)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "123"},
        files={
            "credential_file": (
                "app.pem",
                io.BytesIO(b"-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----"),
                "application/x-pem-file",
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "invalid_key"


async def test_guided_github_app_validate_outage_is_github_unreachable(monkeypatch):
    monkeypatch.setattr(
        github_app, "_app_jwt_client_for", lambda app_id, private_key_b64: "fake-client"
    )

    def _raise(client):
        raise RuntimeError(
            "GitHub App installations lookup failed with 503 ({'message': 'unavailable'})"
        )

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "123"},
        files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
    )
    assert resp.json()["error"] == "github_unreachable"


async def test_guided_github_app_validate_transport_failure_is_github_unreachable(monkeypatch):
    monkeypatch.setattr(
        github_app, "_app_jwt_client_for", lambda app_id, private_key_b64: "fake-client"
    )

    def _raise(client):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/github_app/validate",
        data={"app_id": "123"},
        files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "github_unreachable"


async def test_guided_gemini_apply_also_sets_slot_config_model(monkeypatch, db):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "push_env_var", lambda *a, **k: None)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    store.set_slot_config(
        "gemini", 0,
        model="stale-model", vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-01-01T00:00:00+00:00",
    )
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/gemini/apply",
        json={"slot": 0, "credential": {"api_key": "the-key"}, "model": "gemini-flash-latest"},
    )
    assert resp.status_code == 200
    assert store.get_slot_config("gemini", 0)["model"] == "gemini-flash-latest"


async def test_validate_vertex_model_no_credential_configured(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(vertex_credentials, "resolve_service_account_info", lambda index: None)
    monkeypatch.setattr(settings, "vertex_gcp_project", "")
    client = await _client()
    resp = await client.post("/api/environment/validate/VERTEX_MODEL", json={"value": "x"})
    assert resp.status_code == 200
    assert resp.json()["error"] == "no_credential_configured"


async def test_validate_gcp_project_no_credential_configured_does_not_hit_network(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(vertex_credentials, "resolve_service_account_info", lambda index: None)
    monkeypatch.setattr(settings, "vertex_gcp_project", "")

    def _boom(*a, **k):
        raise AssertionError("list_vertex_models must not be called with no credential at all")

    monkeypatch.setattr(catalog, "list_vertex_models", _boom)
    client = await _client()
    resp = await client.post(
        "/api/environment/validate/VERTEX_GCP_PROJECT", json={"value": "new-proj"}
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "no_credential_configured"


async def test_validate_vertex_model_corrupt_credential_is_structural_error(monkeypatch):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})

    def _raise(index):
        raise ValueError("bad base64")

    monkeypatch.setattr(vertex_credentials, "resolve_service_account_info", _raise)
    client = await _client()
    resp = await client.post("/api/environment/validate/VERTEX_MODEL", json={"value": "x"})
    assert resp.status_code == 200
    assert resp.json()["error"] == "invalid_service_account_json"


async def test_credential_models_refresh_vertex_corrupt_credential_is_structural_error(
    monkeypatch,
):
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 0})

    def _raise(index):
        raise ValueError("bad base64")

    monkeypatch.setattr(vertex_credentials, "resolve_service_account_info", _raise)
    client = await _client()
    resp = await client.get("/api/environment/credential/vertex/models")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "models": None, "error": "invalid_service_account_json"}


async def test_patch_render_survives_a_raising_validator_instead_of_500ing(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "push_env_var", lambda *a, **k: None)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")

    def _boom():
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr(store, "get_all_key_index_overrides", _boom)
    client = await _client()
    resp = await client.patch(
        "/api/environment/render",
        json={"sets": {"GEMINI_MODEL": "whatever", "OTHER_KEY": "fine"}, "deletes": []},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert {"key": "GEMINI_MODEL", "error": "failed_validation"} in result["failed"]
    assert "OTHER_KEY" in result["applied"]


async def test_patch_render_bulk_delete_of_dependent_slot_is_rejected(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _boom(*a, **k):
        raise AssertionError("delete_env_var must not be called for a dependent slot")

    monkeypatch.setattr(render_client, "delete_env_var", _boom)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"gemini": 2})
    monkeypatch.setattr(store, "get_provider_override", lambda: None)
    monkeypatch.setattr(store, "get_slot_config", lambda family, index: None)
    client = await _client()
    resp = await client.patch(
        "/api/environment/render", json={"sets": {}, "deletes": ["GEMINI_API_KEY_2"]}
    )
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "GEMINI_API_KEY_2", "error": "has_dependents"}]


async def test_patch_render_bulk_delete_of_non_dependent_slot_still_works(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    deleted = []
    monkeypatch.setattr(
        render_client, "delete_env_var", lambda service_id, key: deleted.append(key)
    )
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {})
    monkeypatch.setattr(store, "get_provider_override", lambda: None)
    monkeypatch.setattr(store, "get_slot_config", lambda family, index: None)
    client = await _client()
    resp = await client.patch(
        "/api/environment/render", json={"sets": {}, "deletes": ["GEMINI_API_KEY_2"]}
    )
    body = resp.json()
    assert body["applied"] == ["GEMINI_API_KEY_2"]
    assert deleted == ["GEMINI_API_KEY_2"]
