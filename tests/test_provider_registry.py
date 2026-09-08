"""providers/registry.py -- the single provider -> env-var-name mapping,
shared by the root-level review engine (credential resolution) and scripts/
(deploy checks, CLI overrides). Replaces what was previously
scripts/deploy.py's private _PROVIDERS dict -- the review engine now needs
the same mapping, and it must not import from scripts/."""
from __future__ import annotations

from providers import registry
from scripts import deploy


def test_registry_lists_all_providers():
    assert set(registry.PROVIDERS) == {"gemini", "groq", "vertex"}


def test_registry_maps_each_provider_to_its_credential_and_model_env_vars():
    assert registry.PROVIDERS["gemini"] == ("GEMINI_API_KEY", "GEMINI_MODEL")
    assert registry.PROVIDERS["groq"] == ("GROQ_API_KEY", "GROQ_MODEL")
    assert registry.PROVIDERS["vertex"] == ("VERTEX_GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL")


def test_registry_lists_a_key_index_column_per_provider():
    assert set(registry.KEY_INDEX_COLUMNS) == {"gemini", "groq", "vertex"}
    assert registry.KEY_INDEX_COLUMNS["gemini"] == "gemini_key_index"
    assert registry.KEY_INDEX_COLUMNS["groq"] == "groq_key_index"
    assert registry.KEY_INDEX_COLUMNS["vertex"] == "vertex_key_index"


def test_known_providers_matches_the_registry():
    """dashboard/ builds its per-provider backoff panel from
    KNOWN_PROVIDERS; a provider in one and not the other renders a panel that
    silently omits a real provider."""
    from providers.base import KNOWN_PROVIDERS

    assert set(KNOWN_PROVIDERS) == set(registry.PROVIDERS)


def test_deploy_script_imports_the_shared_registry():
    """scripts/deploy.py must not keep its own copy of this mapping -- a
    provider added to one and not the other is exactly the drift this
    registry exists to prevent (see _PROVIDERS's own prior docstring, which
    already called it 'the single source of truth')."""
    assert deploy._PROVIDERS is registry.PROVIDERS


def test_vertex_owns_its_own_model_var():
    """gemini and vertex shared GEMINI_MODEL, but gemini-flash-latest does not
    exist in Vertex's catalog (404) -- so the shared var made the redeploy-free
    provider flip guaranteed-broken. Each provider owns its model."""
    from providers import registry

    assert registry.PROVIDERS["vertex"][1] == "VERTEX_MODEL"
    assert registry.PROVIDERS["gemini"][1] == "GEMINI_MODEL"
    assert registry.PROVIDERS["groq"][1] == "GROQ_MODEL"
    model_vars = [model for _, model in registry.PROVIDERS.values()]
    assert len(model_vars) == len(set(model_vars)), "two providers share a model var"


def test_slot_env_name_is_the_single_naming_seam():
    from providers import registry

    assert registry.slot_env_name("groq", 0) == "GROQ_API_KEY"
    assert registry.slot_env_name("groq", 2) == "GROQ_API_KEY_2"
    assert registry.slot_env_name("vertex", 1) == "VERTEX_GCP_SERVICE_ACCOUNT_KEY_1"
