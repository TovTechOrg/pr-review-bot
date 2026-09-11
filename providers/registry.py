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

# Whether a model must be proven callable -- not merely listed -- before it
# is written into slot_config, per provider. Read by providers/model_check.py
# (the predicate every writer calls) and published verbatim by
# scripts/gen_contract.py so the provisioner implements the same rule rather
# than a copy of its reasoning.
#
# Vertex is the reason this exists: client.models.list() returns the global
# Model Garden, not a per-project entitlement list, so "in the catalog" never
# meant "this project can call it" -- see
# docs/superpowers/specs/2026-09-11-vertex-model-entitlement-validation-design.md
# section 1. Gemini's AI-Studio listing IS key-scoped, but it exposes the same
# free countTokens call, so it is probed too rather than trusted.
#
# groq carries an explicit False with a reason rather than being omitted: an
# absent entry reads as an oversight, an explicit one reads as a decision.
MODEL_PROBE_POLICY = {
    "gemini": {
        "required_before_write": True,
        "mechanism": "count_tokens",
        "reason": None,
    },
    "groq": {
        "required_before_write": False,
        "mechanism": None,
        "reason": (
            "no free token-counting endpoint -- probing would mean a paid "
            "chat completion on every model save, and groq's own model "
            "listing is key-scoped, unlike Vertex's"
        ),
    },
    "vertex": {
        "required_before_write": True,
        "mechanism": "count_tokens",
        "reason": None,
    },
}

# The only two verdicts a probe may report. Kept apart on purpose:
# model_not_callable means the model answered 404 and is unusable here;
# model_probe_unavailable means no verdict was reached (429, 5xx, timeout).
# Collapsing the second into the first would condemn a working model because
# a provider hiccuped -- the same class of wrong answer this whole design
# exists to stop giving.
MODEL_PROBE_ERROR_CODES = ("model_not_callable", "model_probe_unavailable")


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
