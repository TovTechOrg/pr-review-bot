"""Is this model actually usable for this slot? -- the one predicate every
slot_config writer calls.

One predicate rather than a check per caller, for the reason root CLAUDE.md
already records against store.py's cooldown/usage-cap writers: a rule that
lives with one caller is a validation gap waiting for its second caller.
dashboard/environment.py had two slot_config writers and neither checked the
model at all; the interactive validate endpoint that did check was a separate
call the frontend chose to make first. Every writer now gates here --
the guided-setup Apply, the per-slot config PATCH, the arming paths, and
scripts/set_override.py -- so none of them can disagree about what a usable
model is.

Lives in providers/ rather than dashboard/ because scripts/set_override.py is
one of its callers, and the CLI must not depend on the dashboard package.

See docs/superpowers/specs/2026-09-11-vertex-model-entitlement-validation-
design.md.
"""

from __future__ import annotations

from providers import catalog, credentials, registry, vertex_credentials


def problems(
    provider: str,
    slot: int,
    model: str,
    vertex_gcp_project: str | None = None,
    vertex_gcp_location: str | None = None,
    credential: str | dict | None = None,
) -> list[str]:
    """Every reason `model` is unusable for this slot, as structural error
    codes. An empty list means usable.

    `credential` left None resolves the slot's own stored credential, which
    is what the config panel, the CLI, and any re-validate of an
    already-configured slot want. Passed explicitly, it is probed instead --
    guided setup REQUIRES this: it holds a freshly-uploaded credential in its
    own request body and must probe before pushing it to Render, so at probe
    time that credential exists nowhere the slot could resolve it from.
    Resolving the slot there would silently probe the previous credential, or
    none at all on a first-time setup. Shape is whatever the family already
    uses: a raw API-key string for gemini, the decoded service-account dict
    for vertex.

    Fails closed. A probe that could not run (model_probe_unavailable) is a
    problem, not a pass: an operator save is a retryable foreground action,
    and letting an unverified model through on a transient 429 would leave a
    "saved unverified" state that nothing ever re-checks.
    """
    if provider not in registry.PROVIDERS:
        return ["unknown_provider"]
    if not model:
        return ["model_required"]
    if not registry.MODEL_PROBE_POLICY[provider]["required_before_write"]:
        # groq: no free token-counting endpoint, and its own listing is
        # key-scoped -- see registry.MODEL_PROBE_POLICY's reason field.
        return []

    if provider == "vertex":
        info = credential
        if info is None:
            try:
                info = vertex_credentials.resolve_service_account_info(slot)
            except ValueError:
                # Covers json.JSONDecodeError, binascii.Error and
                # UnicodeDecodeError too -- all ValueError subclasses.
                return ["invalid_service_account_json"]
            if info is None and not vertex_gcp_project:
                # No explicit key AND no project: nothing for implicit ADC to
                # resolve against. Mirrors dashboard's _safe_resolve_vertex_info
                # and providers/factory.py's own definition of "configured".
                return ["no_credential_configured"]
        result = catalog.probe_vertex_model(
            info,
            model,
            project_override=vertex_gcp_project or None,
            location_override=vertex_gcp_location or None,
        )
    else:
        api_key = credential
        if api_key is None:
            _, api_key = credentials.resolve(provider, slot)
        if not api_key:
            return ["no_credential_configured"]
        result = catalog.probe_gemini_model(api_key, model)

    if result.ok:
        return []
    return [result.error or "model_probe_unavailable"]
