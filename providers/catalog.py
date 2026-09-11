"""Live model-catalog listing per LLM provider, for the dashboard's guided
credential setup/replace flow and its per-provider model picker.

Each function makes exactly one deliberate live listing call, synchronously
(matching render_client.py's and github_app.py's sync style, so
dashboard/environment.py can wrap these in asyncio.to_thread like every
other write path it already has).

See docs/superpowers/specs/2026-09-03-dashboard-env-credential-guardrails-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from google import genai
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import AuthorizedSession
from google.genai import types
from google.oauth2 import service_account
from groq import Groq

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_LIST_TIMEOUT_MS = 10_000


@dataclass
class CatalogResult:
    ok: bool
    models: list[str] | None
    error: str | None


def _classify_status(status: int | None) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    return "provider_unreachable"


def _status_of(exc: Exception) -> int | None:
    # Duck-typed on purpose: rather than depend on each SDK's own exception
    # class hierarchy (google-genai's and groq's differ, and either could
    # change shape across versions), read whichever HTTP-status-shaped
    # attribute is present. .status_code checked first: some SDK generations
    # (Stainless-based ones, e.g. openai-python's APIError) set a STRING
    # error code on `.code` (e.g. "invalid_api_key") that would otherwise
    # short-circuit an `or` chain and silently misclassify a real 401/429 as
    # provider_unreachable. Falls back to exc.response.status_code last --
    # requests.exceptions.HTTPError (raised by list_accessible_projects'
    # raise_for_status()) carries its status there instead of on the
    # exception itself.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
    return status if isinstance(status, int) else None


def _classify_exception(exc: Exception) -> str:
    return _classify_status(_status_of(exc))


def _classify_vertex_auth_exception(exc: Exception) -> str | None:
    """Vertex auth failures come from google.auth, not an HTTP status on the
    genai SDK's own exception -- a revoked/disabled service-account key, a
    project without the Vertex API enabled, or missing ADC all raise here
    with neither `.code` nor `.status_code`, so _classify_exception alone
    always falls through to provider_unreachable for these. Returns None
    (defer to _classify_exception) for anything not from google.auth.
    """
    if isinstance(
        exc, (google_auth_exceptions.RefreshError, google_auth_exceptions.DefaultCredentialsError)
    ):
        return "unauthorized"
    if isinstance(exc, google_auth_exceptions.GoogleAuthError):
        return "provider_unreachable"
    return None


def _list_generative_models(client: genai.Client) -> list[str]:
    """Every model the client lists, filtered to generateContent-capable ones
    where that capability is known.

    Vertex responses never populate Model.supported_actions
    (_Model_from_vertex has no mapping for it), so a Vertex model is always
    let through rather than dropped -- dropping it would silently empty the
    entire Vertex catalog regardless of credential.

    WHAT THIS LIST IS NOT: for Vertex it is essentially the global Model
    Garden, not a per-project entitlement list -- a live listing returned
    veo-*, lyria-* and medgemma entries this project plainly cannot call.
    Membership here means the credential authenticates. Whether this project
    may actually generate with a given model is probe_vertex_model's
    question, and only a real call can answer it. Treating the two as the
    same fact put a 404-ing model into production -- see the design spec's
    section 1.
    """
    names: list[str] = []
    for model in client.models.list():
        name = model.name or ""
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        actions = getattr(model, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        names.append(name)
    return names


def list_gemini_models(api_key: str) -> CatalogResult:
    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS)
        )
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001 -- classified into a structural error below
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=models, error=None)


def list_groq_models(api_key: str) -> CatalogResult:
    try:
        client = Groq(api_key=api_key, max_retries=0, timeout=10.0)
        response = client.models.list()
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=[m.id for m in response.data], error=None)


DEFAULT_VERTEX_LOCATION = "us-central1"


def _vertex_client(
    service_account_info: dict | None,
    project_override: str | None,
    location_override: str | None,
) -> tuple[genai.Client | None, str | None]:
    """The Vertex client for this (credential, project, location), or a
    structural error code. Shared by list_vertex_models and
    probe_vertex_model so the two can never disagree about which project,
    location or credential a verdict was obtained against -- the listing
    saying one thing and the probe another would be worse than either alone.

    `location_override` unset falls back to DEFAULT_VERTEX_LOCATION -- a
    LITERAL, not a Settings read. settings.vertex_gcp_location is no longer
    authoritative (it's only a seed value for slot_config -- see
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-
    design.md section 10.4) and doesn't exist as an env var on the deployed
    service at all, so reading it here silently used whatever the operator's
    local .env.config happened to say. This fallback exists only for these
    read-only catalog/probe calls; the review path (providers/factory.py)
    has no location fallback at all, by design.
    """
    project = project_override or (service_account_info or {}).get("project_id", "")
    if not project:
        return None, "invalid_service_account_json"
    location = location_override or DEFAULT_VERTEX_LOCATION

    creds = None
    if service_account_info is not None:
        try:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=_VERTEX_SCOPES
            )
        except Exception:  # noqa: BLE001 -- malformed key content, not an HTTP failure
            return None, "invalid_service_account_json"

    return (
        genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS),
        ),
        None,
    )


def list_vertex_models(
    service_account_info: dict | None,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    """Every model Vertex lists for this credential -- which is NOT the same
    as every model this project may call: see _list_generative_models.
    Client construction, including the location fallback, is _vertex_client's."""
    try:
        client, error = _vertex_client(
            service_account_info, project_override, location_override
        )
        if error:
            return CatalogResult(ok=False, models=None, error=error)
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001
        error = _classify_vertex_auth_exception(exc) or _classify_exception(exc)
        return CatalogResult(ok=False, models=None, error=error)
    return CatalogResult(ok=True, models=models, error=None)


# What a probe sends. One token of throwaway text: countTokens neither
# generates nor bills, so the content is irrelevant -- only whether the
# publisher model resolves for this project.
_PROBE_CONTENTS = "ping"


def _classify_probe_exception(exc: Exception) -> str:
    """A probe failure's structural code.

    The 404 branch is the whole point: Vertex answers a model this project
    may not call with 404 NOT_FOUND, byte-for-byte the error a real
    generateContent gets (verified live -- see the design spec's section
    1a). Every other failure means no verdict was reached, which is NOT the
    same answer and must never be reported as one. 401/403 keep their
    credential-shaped classification so an operator is not told to change
    the model when the key is the problem.
    """
    auth = _classify_vertex_auth_exception(exc)
    if auth is not None:
        return auth
    status = _status_of(exc)
    if status == 404:
        return "model_not_callable"
    if status in (401, 403):
        return _classify_status(status)
    return "model_probe_unavailable"


def probe_vertex_model(
    service_account_info: dict | None,
    model: str,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    """Whether `model` is actually callable for this project/location.

    THE question list_vertex_models cannot answer. Vertex's own listing is
    the global Model Garden -- entitlement is enforced only when a request
    names the publisher model -- so membership in it proves the credential
    authenticates and nothing more. countTokens resolves the publisher model
    exactly as generateContent does, for free and without generating
    anything, which makes it the cheapest honest answer available.

    `models` is always None: this reports a verdict, not a catalog.
    """
    try:
        client, error = _vertex_client(
            service_account_info, project_override, location_override
        )
        if error:
            return CatalogResult(ok=False, models=None, error=error)
        client.models.count_tokens(model=model, contents=_PROBE_CONTENTS)
    except Exception as exc:  # noqa: BLE001 -- classified structurally above
        return CatalogResult(ok=False, models=None, error=_classify_probe_exception(exc))
    return CatalogResult(ok=True, models=None, error=None)


def probe_gemini_model(api_key: str, model: str) -> CatalogResult:
    """Whether `model` is callable with this AI-Studio key.

    Gemini's listing IS key-scoped, unlike Vertex's, so this is a narrower
    guarantee than probe_vertex_model's -- but countTokens is free here too,
    and a verdict obtained the same way the review path obtains its own beats
    one inferred from a listing.
    """
    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS)
        )
        client.models.count_tokens(model=model, contents=_PROBE_CONTENTS)
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_probe_exception(exc))
    return CatalogResult(ok=True, models=None, error=None)


# Vertex AI's generative-model regions -- unlike the project dropdown, this
# does NOT come from a live listing call: there is no API that enumerates
# "locations with generative models enabled" keyed by project, and region
# availability is essentially the same across projects (only quota/org
# policy varies per project-location pair, which the guided-setup/config-
# panel flows already re-check live via list_vertex_models before Save/
# Apply). This is a curated reference list, not a guarantee -- if it drifts
# from Google's actual supported-region list, the worst case is a dropdown
# entry that fails that live re-check, never a silently-accepted bad value.
VERTEX_CATALOG_LOCATIONS = [
    "us-central1",
    "us-east1",
    "us-east4",
    "us-east5",
    "us-south1",
    "us-west1",
    "us-west4",
    "northamerica-northeast1",
    "southamerica-east1",
    "europe-west1",
    "europe-west2",
    "europe-west3",
    "europe-west4",
    "europe-west8",
    "europe-west9",
    "europe-southwest1",
    "europe-central2",
    "europe-north1",
    "asia-east1",
    "asia-northeast1",
    "asia-northeast3",
    "asia-south1",
    "asia-southeast1",
    "australia-southeast1",
    "global",
]

_RESOURCE_MANAGER_SEARCH_URL = "https://cloudresourcemanager.googleapis.com/v3/projects:search"


def list_accessible_projects(service_account_info: dict | None) -> CatalogResult:
    """Every GCP project this credential's own IAM bindings let it act
    against, via Cloud Resource Manager's `projects:search`.

    This is a real listing call, not a value derivable from the key itself:
    a service account's `project_id` field names only its "home" project,
    but the same account can hold IAM roles (and therefore be usable by
    providers/factory.py's `vertex_gcp_project`) on other projects too --
    see providers/factory.py's `project=` passthrough, which is never
    validated against the key's own project_id. One deliberate live call,
    same discipline as list_vertex_models/list_gemini_models/
    list_groq_models."""
    if not service_account_info:
        return CatalogResult(ok=False, models=None, error="invalid_service_account_json")
    try:
        creds = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=_VERTEX_SCOPES
        )
    except Exception:  # noqa: BLE001 -- malformed key content, not an HTTP failure
        return CatalogResult(ok=False, models=None, error="invalid_service_account_json")

    try:
        session = AuthorizedSession(creds)
        response = session.get(_RESOURCE_MANAGER_SEARCH_URL, timeout=_LIST_TIMEOUT_MS / 1000)
        response.raise_for_status()
        projects = sorted(
            {
                p["projectId"]
                for p in response.json().get("projects", [])
                if p.get("projectId")
            }
        )
    except Exception as exc:  # noqa: BLE001
        error = _classify_vertex_auth_exception(exc) or _classify_exception(exc)
        return CatalogResult(ok=False, models=None, error=error)
    return CatalogResult(ok=True, models=projects, error=None)
