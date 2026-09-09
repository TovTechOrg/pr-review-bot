"""Single source of truth for provider -> env-var-name mappings.

Both the root app (runtime credential resolution: credentials.py, factory.py)
and scripts/ (deploy verification, the set_provider/set_api_key CLIs) read
this. Previously duplicated as scripts/deploy.py's private _PROVIDERS
dict; moved here because the root app now needs the same mapping and must
not import from scripts/ (the dependency direction runs the other way
everywhere else in this codebase).
"""

from __future__ import annotations

# provider -> (credential env var, model env var)
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    # vertex's credential is a base64-encoded service-account JSON key, not an
    # API-key string -- but it is resolved through the same numbered-slot
    # mechanism (credentials.resolve), so it belongs in the same table.
    # providers/vertex_credentials.py layers the implicit-ADC fallback on
    # top of what this entry resolves.
    #
    # VERTEX_MODEL, not GEMINI_MODEL: vertex and gemini are the same SDK but
    # different model catalogs -- gemini-flash-latest does not exist as a
    # Vertex publisher model (404). Sharing one var made a DB provider flip
    # between them guaranteed-broken. Completes the split whose reasoning
    # config.py already records for GROQ_MODEL.
    "vertex": ("VERTEX_GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL"),
}

# provider -> the runtime_config column holding its active API-key-slot
# index override. A hardcoded whitelist, not a naming convention derived at
# call time -- every SQL statement that touches one of these columns looks
# the name up through this dict rather than building it from a caller's
# `provider` string, so this dict IS the injection guard for those callers.
KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "vertex": "vertex_key_index",
}

def slot_env_name(provider: str, index: int) -> str:
    """The env-var name for `provider`'s API-key slot `index`.

    THE single place the `{base}` / `{base}_{n}` naming scheme is written down.
    It was previously reconstructed independently in providers/credentials.py
    and scripts/_override.py, which meant the scheme had no seam to change --
    and a future credential store (one secret per file, say) would have been a
    sweep instead of a one-module edit.

    Index 0 is the base, unsuffixed var; indices >= 1 are the numbered slots.
    """
    base, _ = PROVIDERS[provider]
    return base if index == 0 else f"{base}_{index}"
