"""Dashboard Environment tab: fetch/edit Render env vars and runtime_config
overrides. The one place `dashboard/` writes anything -- dashboard/router.py
stays read-only (see dashboard/CLAUDE.md). See docs/superpowers/specs/
2026-09-02-dashboard-environment-tab-design.md.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

import config_deps
import github_app
import render_client
from config_deps import CREDENTIAL_FAMILIES, MAX_CREDENTIAL_SLOTS, credential_slot_vars
from providers import catalog, credentials, registry, vertex_credentials
from providers.registry import slot_env_name
from review_queue import dispatcher_tuning_config, store

logger = logging.getLogger(__name__)

router = APIRouter()

_LLM_PROVIDER_FAMILIES = ("gemini", "groq", "vertex")


def _build_render_payload() -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"vars": [], "available_key_slots": {p: [] for p in _LLM_PROVIDER_FAMILIES}}
    values = render_client.env_vars(service_id)
    available_key_slots = {
        provider: [
            i for i, var in enumerate(credential_slot_vars(provider)) if var in values
        ]
        for provider in _LLM_PROVIDER_FAMILIES
    }
    return {
        "vars": [
            {"key": key, "value": value, "protected": key in render_client.PROTECTED_ENV_KEYS}
            for key, value in values.items()
        ],
        "available_key_slots": available_key_slots,
    }


@router.get("/api/environment/render")
async def get_render_env_vars() -> JSONResponse:
    payload = await asyncio.to_thread(_build_render_payload)
    return JSONResponse(payload)


class EnvironmentRenderPatch(BaseModel):
    sets: dict[str, str] = {}
    deletes: list[str] = []


_DIRECT_EDIT_VARS = {
    "GEMINI_MODEL": "gemini",
    "GROQ_MODEL": "groq",
    "VERTEX_MODEL": "vertex",
    "VERTEX_GCP_PROJECT": "vertex",
    "VERTEX_GCP_LOCATION": "vertex",
}


class ValidateVarRequest(BaseModel):
    value: str


def _safe_resolve_vertex_info(slot: int) -> tuple[dict | None, str | None]:
    """Resolve the Vertex service-account info for `slot`, never raising.

    Returns (info, error). A non-None `error` is a structural code and
    `info` is always None in that case. `info is None` with no error means
    "no explicit key -- fall through to implicit ADC", mirroring
    providers/factory.py's own definition of "configured": a missing key
    is only a problem when the slot's own vertex_gcp_project isn't set
    either, since without either there is nothing for ADC to resolve
    against. Reads slot_config, not settings.vertex_gcp_project: that env
    var has been DB-only since the 2026-09-08 slotted-config work and no
    longer exists on the deployed service, so checking it here always read
    as unset and reported no_credential_configured for a perfectly-
    configured ADC-plus-slot_config setup.
    """
    try:
        info = vertex_credentials.resolve_service_account_info(slot)
    except ValueError:
        # Covers json.JSONDecodeError, binascii.Error, and UnicodeDecodeError
        # too -- all are ValueError subclasses.
        return None, "invalid_service_account_json"
    if info is None:
        slot_project = (store.get_slot_config("vertex", slot) or {}).get("vertex_gcp_project")
        if not slot_project:
            return None, "no_credential_configured"
    return info, None


def _validate_model_var(provider: str, candidate: str) -> dict:
    if provider == "vertex":
        slot = store.get_all_key_index_overrides().get("vertex", 0)
        info, error = _safe_resolve_vertex_info(slot)
        if error:
            return {"ok": False, "error": error, "models": None}
        row = store.get_slot_config("vertex", slot) or {}
        result = catalog.list_vertex_models(
            info,
            project_override=row.get("vertex_gcp_project") or None,
            location_override=row.get("vertex_gcp_location") or None,
        )
    else:
        slot = store.get_all_key_index_overrides().get(provider, 0)
        _, api_key = credentials.resolve(provider, slot)
        if not api_key:
            return {"ok": False, "error": "no_credential_configured", "models": None}
        result = (
            catalog.list_gemini_models(api_key)
            if provider == "gemini"
            else catalog.list_groq_models(api_key)
        )
    if not result.ok:
        return {"ok": False, "error": result.error, "models": None}
    if candidate not in (result.models or []):
        return {"ok": False, "error": "not_in_catalog", "models": result.models}
    return {"ok": True, "error": None, "models": result.models}


def _validate_gcp_var(var: str, candidate: str) -> dict:
    slot = store.get_all_key_index_overrides().get("vertex", 0)
    info, error = _safe_resolve_vertex_info(slot)
    if error:
        return {"ok": False, "error": error, "models": None}
    kwargs = (
        {"project_override": candidate}
        if var == "VERTEX_GCP_PROJECT"
        else {"location_override": candidate}
    )
    result = catalog.list_vertex_models(info, **kwargs)
    return {"ok": result.ok, "error": result.error, "models": None}


def _validate_var(var: str, candidate: str) -> dict:
    provider = _DIRECT_EDIT_VARS[var]
    if var in ("VERTEX_GCP_PROJECT", "VERTEX_GCP_LOCATION"):
        return _validate_gcp_var(var, candidate)
    return _validate_model_var(provider, candidate)


@router.post("/api/environment/validate/{var}")
async def validate_var(var: str, payload: ValidateVarRequest) -> JSONResponse:
    if var not in _DIRECT_EDIT_VARS:
        raise HTTPException(status_code=404, detail="not a directly-validatable var")
    result = await asyncio.to_thread(_validate_var, var, payload.value)
    return JSONResponse(result)


def _validate_llm_credential(family: str, api_key: str) -> dict:
    result = (
        catalog.list_gemini_models(api_key)
        if family == "gemini"
        else catalog.list_groq_models(api_key)
    )
    return {
        "ok": result.ok,
        "error": result.error,
        "models": result.models,
        "project_id": None,
        "installation_id": None,
        "conflicts": [],
    }


def _validate_vertex_credential(raw_bytes: bytes, slot: int) -> dict:
    try:
        info = json.loads(raw_bytes.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ok": False,
            "error": "invalid_service_account_json",
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    project_id = info.get("project_id") if isinstance(info, dict) else None
    # Validate the uploaded key against ITS OWN project, not whatever the
    # slot's current vertex_gcp_project happens to be -- a replacement key
    # for a different (but perfectly valid) project must not be rejected just
    # because the slot's existing project hasn't been updated yet. The
    # mismatch itself is surfaced separately below, as a conflict prompt
    # rather than a validation failure.
    result = catalog.list_vertex_models(info, project_override=project_id)
    conflicts: list[dict] = []
    if result.ok and project_id:
        # slot_config, not Render: VERTEX_GCP_PROJECT has been DB-only since
        # the 2026-09-08 slotted-config work, so reading it off the Render
        # service always returned None -- and this conflict prompt, the whole
        # reason config_deps.conflicts_for exists, could never fire.
        current_project = (store.get_slot_config("vertex", slot) or {}).get(
            "vertex_gcp_project"
        )
        conflicts = config_deps.conflicts_for("vertex", project_id, current_project)
    return {
        "ok": result.ok,
        "error": result.error,
        "models": result.models,
        "project_id": project_id if result.ok else None,
        "installation_id": None,
        "conflicts": conflicts,
    }


_GITHUB_STATUS_RE = re.compile(r"failed with (\d+)")


def _classify_github_runtime_error(exc: RuntimeError) -> str:
    message = str(exc)
    if "multiple installations" in message:
        return "multiple_installations"
    match = _GITHUB_STATUS_RE.search(message)
    status = int(match.group(1)) if match else None
    if status is not None and status >= 500:
        return "github_unreachable"
    return "unauthorized"


def _validate_github_app_credential(app_id: int, raw_bytes: bytes) -> dict:
    if not raw_bytes:
        return {
            "ok": False,
            "error": "invalid_key",
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    private_key_b64 = base64.b64encode(raw_bytes).decode()
    empty = {
        "ok": False,
        "models": None,
        "project_id": None,
        "installation_id": None,
        "conflicts": [],
    }
    try:
        gh_client = github_app._app_jwt_client_for(app_id, private_key_b64)
        installation_id = github_app.discover_installation_id_for_app(client=gh_client)
    except github_app.AppNotInstalledError:
        return {**empty, "error": "installation_not_found"}
    except RuntimeError as exc:
        return {**empty, "error": _classify_github_runtime_error(exc)}
    except (ValueError, AssertionError, jwt.exceptions.PyJWTError):
        return {**empty, "error": "invalid_key"}
    except Exception:  # noqa: BLE001 -- transport-level failures (DNS, connection
        # refused) aren't GithubException and would otherwise 500
        logger.exception("environment: github app validation failed unexpectedly")
        return {**empty, "error": "github_unreachable"}
    return {
        "ok": True,
        "error": None,
        "models": None,
        "project_id": None,
        "installation_id": installation_id,
        "conflicts": [],
    }


@router.post("/api/environment/credential/{family}/validate")
async def validate_credential(
    family: str,
    api_key: str | None = Form(None),
    app_id: int | None = Form(None),
    slot: int = Form(0),
    credential_file: UploadFile | None = File(None),
) -> JSONResponse:
    if family not in CREDENTIAL_FAMILIES:
        raise HTTPException(status_code=404, detail="unknown credential family")
    if not (0 <= slot < MAX_CREDENTIAL_SLOTS):
        raise HTTPException(status_code=422, detail="slot out of range")

    if family in ("gemini", "groq"):
        if not api_key:
            raise HTTPException(status_code=422, detail="api_key is required")
        payload = await asyncio.to_thread(_validate_llm_credential, family, api_key)
    elif family == "vertex":
        if credential_file is None:
            raise HTTPException(status_code=422, detail="credential_file is required")
        raw_bytes = await credential_file.read()
        payload = await asyncio.to_thread(_validate_vertex_credential, raw_bytes, slot)
    else:  # github_app
        if app_id is None or credential_file is None:
            raise HTTPException(status_code=422, detail="app_id and credential_file are required")
        raw_bytes = await credential_file.read()
        payload = await asyncio.to_thread(_validate_github_app_credential, app_id, raw_bytes)
    return JSONResponse(payload)


class ApplyLlmCredentialRequest(BaseModel):
    slot: int = Field(default=0, ge=0, lt=MAX_CREDENTIAL_SLOTS)
    credential: dict[str, str]
    model: str
    vertex_gcp_project: str | None = None
    vertex_gcp_location: str | None = None
    clear_vertex_gcp_project: bool = False


class ApplyGithubAppRequest(BaseModel):
    app_id: str
    private_key_b64: str
    installation_id: int


def _apply_llm_credential(family: str, payload: ApplyLlmCredentialRequest) -> dict:
    """Split-push: the credential is the only thing that still goes to
    Render. Model (and for vertex, project/location) is DB-only now --
    store.set_slot_config's contract is all three fields together, never a
    partial write, so this is one upsert call, not per-field pushes. See
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-
    design.md section 8."""
    slot_config_key = f"slot_config.{family}.{payload.slot}"
    # store.set_slot_config's contract is all three fields together, so a
    # request that omits vertex_gcp_project/_location (the common case: the
    # guided flow is normally rotating a credential, not moving GCP regions)
    # must be read-then-merged, NOT blind-written. Blind-writing None here
    # meant every credential rotation through this flow nulled the slot's
    # project/location and left providers/factory.py raising "no location
    # configured" on every subsequent review.
    existing = store.get_slot_config(family, payload.slot) or {}
    vertex_gcp_project = (
        None
        if payload.clear_vertex_gcp_project
        else (payload.vertex_gcp_project or existing.get("vertex_gcp_project"))
    )
    vertex_gcp_location = payload.vertex_gcp_location or existing.get("vertex_gcp_location")
    if family == "vertex" and not vertex_gcp_location:
        # No env fallback for location (spec section 4b/10.4): a slot with no
        # resolvable location cannot run a single review, so refuse up front,
        # BEFORE the Render credential push below, rather than writing a row
        # factory.py will reject later, on a PR.
        return {
            "applied": [],
            "failed": [{"key": slot_config_key, "error": "vertex_gcp_location_required"}],
        }

    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}

    credential_var = slot_env_name(family, payload.slot)
    credential_value = (
        payload.credential.get("service_account_b64", "")
        if family == "vertex"
        else payload.credential.get("api_key", "")
    )
    applied: list[str] = []
    failed: list[dict] = []
    try:
        render_client.push_env_var(service_id, credential_var, credential_value)
        applied.append(credential_var)
    except Exception as exc:  # noqa: BLE001
        failed.append({"key": credential_var, "error": type(exc).__name__})

    try:
        store.set_slot_config(
            family,
            payload.slot,
            model=payload.model,
            vertex_gcp_project=vertex_gcp_project if family == "vertex" else None,
            vertex_gcp_location=vertex_gcp_location if family == "vertex" else None,
            now=datetime.now(timezone.utc).isoformat(),
        )
        applied.append(slot_config_key)
    except Exception as exc:  # noqa: BLE001
        failed.append({"key": slot_config_key, "error": type(exc).__name__})

    if credential_var in applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}


def _apply_github_app_credential(payload: ApplyGithubAppRequest) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}
    applied: list[str] = []
    failed: list[dict] = []
    for key, value in (
        ("GITHUB_APP_ID", payload.app_id),
        ("GITHUB_APP_PRIVATE_KEY", payload.private_key_b64),
        ("GITHUB_APP_INSTALLATION_ID", str(payload.installation_id)),
    ):
        try:
            render_client.push_env_var(service_id, key, value)
            applied.append(key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
    if applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}


@router.post("/api/environment/credential/{family}/apply")
async def apply_credential(family: str, payload: dict) -> JSONResponse:
    if family not in ("gemini", "groq", "vertex", "github_app"):
        raise HTTPException(status_code=404, detail="unknown credential family")
    try:
        if family == "github_app":
            req = ApplyGithubAppRequest.model_validate(payload)
        else:
            req = ApplyLlmCredentialRequest.model_validate(payload)
    except ValidationError:
        # Never surface exc itself: pydantic embeds the full rejected
        # input_value (here, the credential/private key) in its message --
        # this must stay structural, per root CLAUDE.md's rule on
        # secret-bearing validation errors. FastAPI's own 422 handling only
        # covers a RequestValidationError raised by its dependency layer,
        # not one raised inside a handler body like this, so it must be
        # caught explicitly here.
        raise HTTPException(status_code=422, detail="invalid credential payload") from None
    if family == "github_app":
        result = await asyncio.to_thread(_apply_github_app_credential, req)
    else:
        result = await asyncio.to_thread(_apply_llm_credential, family, req)
    return JSONResponse(result)


def _apply_render_patch(payload: EnvironmentRenderPatch) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {
            "applied": [],
            "failed": [{"key": "*", "error": "service_not_found"}],
            "deploy_id": None,
        }

    applied: list[str] = []
    failed: list[dict] = []
    stopped = False

    key_index_overrides = None
    provider_override_value = None
    for key in payload.deletes:
        if stopped:
            break
        if key in render_client.PROTECTED_ENV_KEYS:
            failed.append({"key": key, "error": "protected"})
            continue
        if any(config_deps.slot_index_for_var(f, key) is not None for f in _LLM_PROVIDER_FAMILIES):
            # A credential slot with dependent runtime_config state must go
            # through DELETE /api/environment/render/{key}'s confirm flow --
            # this bulk path has no per-key confirmation step, so it can only
            # ever delete a slot that is already dependency-free.
            if key_index_overrides is None:
                key_index_overrides = store.get_all_key_index_overrides()
                provider_override_value = store.get_provider_override()
            slot_config_row = None
            for family in _LLM_PROVIDER_FAMILIES:
                index = config_deps.slot_index_for_var(family, key)
                if index is not None:
                    slot_config_row = store.get_slot_config(family, index)
                    break
            dependents = config_deps.dependents_of(
                key,
                key_index_overrides=key_index_overrides,
                provider_override=provider_override_value,
                slot_config_row=slot_config_row,
            )
            if dependents is not None and dependents.any():
                failed.append({"key": key, "error": "has_dependents"})
                continue
        try:
            render_client.delete_env_var(service_id, key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: deleted %s", key)

    for key, value in payload.sets.items():
        if stopped:
            break
        if key in _DIRECT_EDIT_VARS:
            try:
                check = _validate_var(key, value)
            except Exception:  # noqa: BLE001 -- a resolution failure is still a validation failure
                logger.exception("environment: validation of %s raised unexpectedly", key)
                failed.append({"key": key, "error": "failed_validation"})
                continue
            if not check["ok"]:
                failed.append({"key": key, "error": "failed_validation"})
                continue
        try:
            render_client.push_env_var(service_id, key, value)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: set %s (len %d)", key, len(value))

    deploy_id = None
    if applied:
        try:
            deploy_id = render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after applying %s", applied)

    return {"applied": applied, "failed": failed, "deploy_id": deploy_id}


@router.patch("/api/environment/render")
async def patch_render_env_vars(payload: EnvironmentRenderPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_render_patch, payload)
    return JSONResponse(result)


def _build_config_payload() -> dict:
    base, cap, factor = store.get_cooldown_overrides()
    tokens, reset = store.get_usage_cap_overrides()
    payload = {
        "provider": store.get_provider_override(),
        "cooldown_base_seconds": base,
        "cooldown_max_seconds": cap,
        "cooldown_factor": factor,
        "usage_cap_tokens": tokens,
        "usage_cap_reset": reset,
        "review_draft_prs": store.get_review_draft_override(),
        "key_index": store.get_all_key_index_overrides(),
    }
    payload.update(store.get_dispatcher_tuning_config())
    payload["slot_configs"] = _slot_configs_by_provider()
    return payload


def _slot_configs_by_provider() -> dict[str, list[dict]]:
    """store.get_all_slot_configs() grouped and sorted for display -- one
    list per provider, each entry a slot's configured (model, and for
    vertex, project/location). Used by both the config payload above (Task
    15) and the read-only per-slot listing (Task 16)."""
    grouped: dict[str, list[dict]] = {p: [] for p in registry.PROVIDERS}
    for (provider, slot), config in store.get_all_slot_configs().items():
        if provider not in grouped:
            continue
        entry = {"slot": slot, "model": config["model"]}
        if provider == "vertex":
            entry["vertex_gcp_project"] = config["vertex_gcp_project"]
            entry["vertex_gcp_location"] = config["vertex_gcp_location"]
        grouped[provider].append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda e: e["slot"])
    return grouped


@router.get("/api/environment/config")
async def get_environment_config() -> JSONResponse:
    payload = await asyncio.to_thread(_build_config_payload)
    return JSONResponse(payload)


def _resolve_current_credential(provider: str, slot: int | None) -> tuple[bool, str]:
    """Resolve the currently-stored credential for `provider`+`slot`.

    Returns (has_credential, value) -- a raw API key for gemini/groq. Not
    used for vertex, which resolves via vertex_credentials instead.
    """
    if slot is None:
        slot = store.get_all_key_index_overrides().get(provider, 0)
    _, value = credentials.resolve(provider, slot)
    return bool(value), value


def _fetch_models_for_provider(provider: str, slot: int | None) -> dict:
    if provider == "vertex":
        if slot is None:
            slot = store.get_all_key_index_overrides().get("vertex", 0)
        info, error = _safe_resolve_vertex_info(slot)
        if error:
            return {"ok": False, "models": None, "error": error}
        # Per-slot project/location: `slot` exists on this function precisely
        # because different credentials see different catalogs, and since the
        # 2026-09-08 slotted-config work those two values live per slot in
        # slot_config rather than in one flat env var.
        row = store.get_slot_config("vertex", slot) or {}
        result = catalog.list_vertex_models(
            info,
            project_override=row.get("vertex_gcp_project") or None,
            location_override=row.get("vertex_gcp_location") or None,
        )
    else:
        has_credential, api_key = _resolve_current_credential(provider, slot)
        if not has_credential:
            return {"ok": False, "models": None, "error": "no_credential_configured"}
        result = (
            catalog.list_gemini_models(api_key)
            if provider == "gemini"
            else catalog.list_groq_models(api_key)
        )
    return {"ok": result.ok, "models": result.models, "error": result.error}


@router.get("/api/environment/credential/{family}/models")
async def get_credential_models(family: str, slot: int | None = None) -> JSONResponse:
    if family not in _LLM_PROVIDER_FAMILIES:
        raise HTTPException(status_code=404, detail="not an LLM provider family")
    payload = await asyncio.to_thread(_fetch_models_for_provider, family, slot)
    return JSONResponse(payload)


_TUNING_KNOB_KEYS = (
    "llm_request_timeout_seconds",
    "dispatcher_default_retry_after_seconds",
    "dispatcher_failure_base_backoff_seconds",
    "dispatcher_failure_max_backoff_seconds",
    "dispatcher_max_failure_attempts",
    "dispatcher_max_notice_post_attempts",
    "dispatcher_min_retry_after_seconds",
    "dispatcher_backoff_jitter_seconds",
    "dispatcher_notice_sweep_batch_size",
    "dispatcher_idle_sleep_seconds",
)


class EnvironmentConfigPatch(BaseModel):
    provider: str | None = None
    cooldown_base_seconds: float | None = None
    cooldown_max_seconds: float | None = None
    cooldown_factor: float | None = None
    usage_cap_tokens: int | None = None
    usage_cap_reset: str | None = None
    review_draft_prs: bool | None = None
    key_index: dict[str, int | None] = {}
    llm_request_timeout_seconds: float | None = None
    dispatcher_default_retry_after_seconds: float | None = None
    dispatcher_failure_base_backoff_seconds: float | None = None
    dispatcher_failure_max_backoff_seconds: float | None = None
    dispatcher_max_failure_attempts: int | None = None
    dispatcher_max_notice_post_attempts: int | None = None
    dispatcher_min_retry_after_seconds: float | None = None
    dispatcher_backoff_jitter_seconds: float | None = None
    dispatcher_notice_sweep_batch_size: int | None = None
    dispatcher_idle_sleep_seconds: float | None = None


def _apply_config_patch(payload: EnvironmentConfigPatch) -> dict:
    # exclude_unset: a field the caller never sent must not be read as
    # "clear this override" -- only a field explicitly present in the
    # request body (even if its value is null) is applied.
    fields = payload.model_dump(exclude_unset=True)
    now = datetime.now(timezone.utc).isoformat()
    applied: list[str] = []
    failed: list[dict] = []

    if "provider" in fields:
        provider = fields["provider"]
        if provider is not None and provider not in registry.PROVIDERS:
            failed.append({"key": "provider", "error": "unknown_provider"})
        else:
            try:
                store.set_provider_override(provider, now)
                applied.append("provider")
            except Exception as exc:  # noqa: BLE001
                failed.append({"key": "provider", "error": type(exc).__name__})

    cooldown_keys = ("cooldown_base_seconds", "cooldown_max_seconds", "cooldown_factor")
    cooldown_fields = {k: fields[k] for k in cooldown_keys if k in fields}
    if cooldown_fields:
        try:
            current_base, current_cap, current_factor = store.get_cooldown_overrides()
            base = cooldown_fields.get("cooldown_base_seconds", current_base)
            cap = cooldown_fields.get("cooldown_max_seconds", current_cap)
            factor = cooldown_fields.get("cooldown_factor", current_factor)
            store.set_cooldown_override(base, cap, factor, now)
            applied.extend(cooldown_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in cooldown_fields)

    usage_keys = ("usage_cap_tokens", "usage_cap_reset")
    usage_fields = {k: fields[k] for k in usage_keys if k in fields}
    if usage_fields:
        try:
            current_tokens, current_reset = store.get_usage_cap_overrides()
            tokens = usage_fields.get("usage_cap_tokens", current_tokens)
            reset = usage_fields.get("usage_cap_reset", current_reset)
            store.set_usage_cap_override(tokens, reset, now)
            applied.extend(usage_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in usage_fields)

    if "review_draft_prs" in fields:
        try:
            store.set_review_draft_override(fields["review_draft_prs"], now)
            applied.append("review_draft_prs")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": "review_draft_prs", "error": type(exc).__name__})

    for provider, index in fields.get("key_index", {}).items():
        if provider not in registry.PROVIDERS:
            failed.append({"key": f"key_index.{provider}", "error": "unknown_provider"})
            continue
        if index is not None and not (0 <= index < MAX_CREDENTIAL_SLOTS):
            failed.append({"key": f"key_index.{provider}", "error": "invalid_slot"})
            continue
        try:
            store.set_key_index_override(provider, index, now)
            applied.append(f"key_index.{provider}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": f"key_index.{provider}", "error": type(exc).__name__})

    tuning_fields = {k: fields[k] for k in _TUNING_KNOB_KEYS if k in fields}
    if tuning_fields:
        current = store.get_dispatcher_tuning_config()
        merged = {**current, **tuning_fields}
        # The 9 knobs have no env fallback and no Settings-side Field()
        # constraint at runtime any more, so this endpoint is the ONLY place
        # a bad value can be caught before it reaches the dispatcher. Rejected
        # as a whole group (they're one upsert) with the offending reasons
        # named, never partially written.
        invalid = dispatcher_tuning_config.problems(merged)
        # dispatcher_idle_sleep_seconds is NOT one of the 9 require_config()
        # validates (it's read through a separate throttled path -- see
        # review_queue/dispatcher.py's run_forever -- not the per-tick
        # tuning cache), so it needs its own check here rather than relying
        # on dispatcher_tuning_config.problems().
        idle_sleep = merged.get("dispatcher_idle_sleep_seconds")
        if idle_sleep is None:
            invalid.append("dispatcher_idle_sleep_seconds is not set")
        elif idle_sleep <= 0:
            invalid.append(
                f"dispatcher_idle_sleep_seconds={idle_sleep!r} would busy-loop the "
                "dispatcher (must be > 0)"
            )
        if invalid:
            failed.extend(
                {"key": k, "error": "invalid_tuning_config: " + "; ".join(invalid)}
                for k in tuning_fields
            )
        else:
            try:
                store.set_dispatcher_tuning_config(**merged, now=now)
                applied.extend(tuning_fields.keys())
            except Exception as exc:  # noqa: BLE001
                failed.extend({"key": k, "error": type(exc).__name__} for k in tuning_fields)

    return {"applied": applied, "failed": failed}


@router.patch("/api/environment/config")
async def patch_environment_config(payload: EnvironmentConfigPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_config_patch, payload)
    return JSONResponse(result)


class SlotConfigPatch(BaseModel):
    provider: str
    slot: int = Field(ge=0, lt=MAX_CREDENTIAL_SLOTS)
    model: str | None = None
    vertex_gcp_project: str | None = None
    vertex_gcp_location: str | None = None


def _apply_slot_config_patch(payload: SlotConfigPatch) -> dict:
    """Edit one slot_config row directly -- this is the config panel's
    per-slot model/project/location editor, replacing the retired
    per-provider `model` field on EnvironmentConfigPatch (see providers/
    registry.py's retired MODEL_COLUMNS and docs/superpowers/specs/
    2026-09-08-slotted-config-and-db-delegation-design.md section 4b).

    Merges over the current row (store.set_slot_config is all-three-together
    by contract), so a caller sending only `model` cannot silently null a
    vertex slot's project/location -- the same trap _apply_llm_credential
    fell into before its own fix. Never touches *_key_index: which slot is
    active is a separate concern (spec section 4a)."""
    if payload.provider not in registry.PROVIDERS:
        return {"applied": [], "failed": [{"key": "provider", "error": "unknown_provider"}]}
    key = f"slot_config.{payload.provider}.{payload.slot}"
    fields = payload.model_dump(exclude_unset=True, exclude={"provider", "slot"})
    existing = store.get_slot_config(payload.provider, payload.slot) or {}
    model = fields.get("model", existing.get("model"))
    project = fields.get("vertex_gcp_project", existing.get("vertex_gcp_project"))
    location = fields.get("vertex_gcp_location", existing.get("vertex_gcp_location"))
    if not model:
        return {"applied": [], "failed": [{"key": key, "error": "model_required"}]}
    if payload.provider == "vertex" and not location:
        return {"applied": [], "failed": [{"key": key, "error": "vertex_gcp_location_required"}]}
    try:
        store.set_slot_config(
            payload.provider,
            payload.slot,
            model=model,
            vertex_gcp_project=project if payload.provider == "vertex" else None,
            vertex_gcp_location=location if payload.provider == "vertex" else None,
            now=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"applied": [], "failed": [{"key": key, "error": type(exc).__name__}]}
    return {"applied": [key], "failed": []}


@router.patch("/api/environment/slot-config")
async def patch_slot_config(payload: SlotConfigPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_slot_config_patch, payload)
    return JSONResponse(result)


def _cascade_delete(key: str, confirm: bool) -> tuple[int, dict]:
    if key in render_client.PROTECTED_ENV_KEYS:
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": "protected"}],
            "deploy_id": None,
        }

    key_index_overrides = store.get_all_key_index_overrides()
    provider_override = store.get_provider_override()
    # dependents_of stays pure/I/O-free (its own module docstring) -- fetch
    # the slot's current slot_config row here and hand it in, same as
    # key_index_overrides/provider_override above.
    slot_config_row = None
    for family in _LLM_PROVIDER_FAMILIES:
        index = config_deps.slot_index_for_var(family, key)
        if index is not None:
            slot_config_row = store.get_slot_config(family, index)
            break
    dependents = config_deps.dependents_of(
        key,
        key_index_overrides=key_index_overrides,
        provider_override=provider_override,
        slot_config_row=slot_config_row,
    )

    if dependents is not None and dependents.any() and not confirm:
        return 409, {"dependents": dependents.labels()}

    service_id = render_client.find_service_id()
    if service_id is None:
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": "service_not_found"}],
            "deploy_id": None,
        }
    try:
        render_client.delete_env_var(service_id, key)
    except Exception as exc:  # noqa: BLE001
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": type(exc).__name__}],
            "deploy_id": None,
        }

    now = datetime.now(timezone.utc).isoformat()
    if dependents is not None:
        for family in _LLM_PROVIDER_FAMILIES:
            index = config_deps.slot_index_for_var(family, key)
            if index is None:
                continue
            if dependents.key_index_override:
                store.set_key_index_override(family, None, now)
            if dependents.provider_override:
                store.set_provider_override(None, now)
            if dependents.slot_config:
                store.delete_slot_config(family, index)
            break

    deploy_id = None
    try:
        deploy_id = render_client.trigger_deploy(service_id)
    except Exception:  # noqa: BLE001
        logger.exception("environment: failed to trigger deploy after cascade delete of %s", key)
    logger.info("environment: deleted %s (cascade)", key)
    return 200, {"applied": [key], "failed": [], "deploy_id": deploy_id}


@router.delete("/api/environment/render/{key}")
async def delete_render_env_var(key: str, confirm: bool = False) -> JSONResponse:
    status_code, payload = await asyncio.to_thread(_cascade_delete, key, confirm)
    return JSONResponse(payload, status_code=status_code)
