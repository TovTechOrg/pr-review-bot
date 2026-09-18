"""In-memory stand-ins for providers.catalog / providers.credentials /
providers.vertex_credentials. Reaches no network.

dashboard/environment.py and providers/model_check.py both call these on the
module object (`from providers import catalog, credentials,
vertex_credentials` then e.g. `catalog.list_gemini_models(...)`), and
demo/app.py::install_mocks() rebinds them there by name -- same technique as
demo/render_client.py and demo/github_app.py. Every function here matches
its real counterpart's signature exactly.

credentials.resolve/vertex_credentials.resolve_service_account_info also
need mocking, not just catalog: the demo service's real GEMINI_API_KEY/
GROQ_API_KEY/VERTEX_GCP_SERVICE_ACCOUNT_KEY are unset (config.py's
defaults), so the config-table row's Validate/model-list calls
(dashboard/environment.py's `_validate_model_var`/`_fetch_models_for_provider`,
and providers/model_check.py's `problems` when no explicit credential is
passed) short-circuit on "no_credential_configured" before ever reaching
catalog -- unless these two also hand back a plausible-looking credential
first.
"""

from __future__ import annotations

from demo.model_catalog import MODELS_BY_PROVIDER
from providers.catalog import CatalogResult

# Plausible-looking, entirely synthetic -- same discipline as
# demo/render_client.py's _ENV_VARS. Nothing here authenticates anything.
DEMO_VERTEX_SERVICE_ACCOUNT_INFO: dict = {
    "type": "service_account",
    "project_id": "demo-project",
    "client_email": "demo@demo-project.iam.gserviceaccount.com",
}


def list_gemini_models(api_key: str) -> CatalogResult:
    return CatalogResult(ok=True, models=list(MODELS_BY_PROVIDER["gemini"]), error=None)


def list_groq_models(api_key: str) -> CatalogResult:
    return CatalogResult(ok=True, models=list(MODELS_BY_PROVIDER["groq"]), error=None)


def list_vertex_models(
    service_account_info: dict | None,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    return CatalogResult(ok=True, models=list(MODELS_BY_PROVIDER["vertex"]), error=None)


def probe_vertex_model(
    service_account_info: dict | None,
    model: str,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    return CatalogResult(ok=True, models=None, error=None)


def probe_gemini_model(api_key: str, model: str) -> CatalogResult:
    return CatalogResult(ok=True, models=None, error=None)


def list_accessible_projects(service_account_info: dict | None) -> CatalogResult:
    project_id = DEMO_VERTEX_SERVICE_ACCOUNT_INFO["project_id"]
    return CatalogResult(ok=True, models=[project_id], error=None)


def resolve(provider: str, index: int) -> tuple[str, str]:
    from providers import registry

    base, _ = registry.PROVIDERS[provider]
    return base, f"demo-{provider}-key-not-real"


def resolve_service_account_info(index: int) -> dict | None:
    return dict(DEMO_VERTEX_SERVICE_ACCOUNT_INFO)
