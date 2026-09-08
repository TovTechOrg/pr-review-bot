"""Dependency graph for env vars grouped into "credential families".

Lets dashboard/environment.py's guided add/replace/delete flow and its
direct-edit validation stay generic across gemini/groq/vertex/github_app
instead of hardcoding per-provider branches. Pure logic, no I/O -- callers
fetch the current runtime_config/Render state and pass it in.

See docs/superpowers/specs/2026-09-03-dashboard-env-credential-guardrails-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from providers.registry import slot_env_name

MAX_CREDENTIAL_SLOTS = 5

# "credential": the var(s) that ARE the identity. "model": the one var this
# family's live model picker writes (LLM providers only). "soft_deps": an
# independent var whose value can become STALE (not absent) when the
# credential changes -- Vertex's VERTEX_GCP_PROJECT/VERTEX_GCP_LOCATION, checked via
# conflicts_for. "derived": a var that is NEVER operator-authored, always
# recomputed from the credential -- GitHub App's installation id.
CREDENTIAL_FAMILIES: dict[str, dict] = {
    "gemini": {"credential": ["GEMINI_API_KEY"], "model": "GEMINI_MODEL"},
    "groq": {"credential": ["GROQ_API_KEY"], "model": "GROQ_MODEL"},
    "vertex": {
        "credential": ["VERTEX_GCP_SERVICE_ACCOUNT_KEY"],
        "model": "VERTEX_MODEL",
        "soft_deps": ["VERTEX_GCP_PROJECT", "VERTEX_GCP_LOCATION"],
    },
    "github_app": {
        "credential": ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"],
        "derived": ["GITHUB_APP_INSTALLATION_ID"],
    },
}

# The only families whose credential occupies numbered slots (GEMINI_API_KEY_1,
# _2, ...) -- github_app has exactly one App identity, no slots.
_SLOTTED_FAMILIES = ("gemini", "groq", "vertex")


def credential_slot_vars(family: str) -> list[str]:
    """Every env var name `family`'s credential can occupy.

    For an LLM provider: the base var plus every numbered slot
    (`GEMINI_API_KEY`, `GEMINI_API_KEY_1`, ... up to MAX_CREDENTIAL_SLOTS-1).
    For github_app: its fixed two-var pair, unchanged (no slots).
    """
    if family not in _SLOTTED_FAMILIES:
        return list(CREDENTIAL_FAMILIES[family]["credential"])
    return [slot_env_name(family, i) for i in range(MAX_CREDENTIAL_SLOTS)]


def slot_index_for_var(family: str, var: str) -> int | None:
    """Which numbered slot `var` is within `family`'s credential, else None.

    Always None for github_app (not slotted) and for any var that isn't a
    member of `family`'s credential vars at all.
    """
    if family not in _SLOTTED_FAMILIES:
        return None
    for index, candidate in enumerate(credential_slot_vars(family)):
        if candidate == var:
            return index
    return None


@dataclass
class DeleteDependents:
    key_index_override: bool = False
    provider_override: bool = False

    def labels(self) -> list[str]:
        labels = []
        if self.key_index_override:
            labels.append("key_index override")
        if self.provider_override:
            labels.append("active provider override")
        return labels

    def any(self) -> bool:
        return self.key_index_override or self.provider_override


def dependents_of(
    var: str,
    *,
    key_index_overrides: dict[str, int],
    provider_override: str | None,
) -> DeleteDependents | None:
    """What runtime_config state would dangle if `var` were deleted.

    Only LLM-provider credential slots have anything to compute: github_app's
    credential vars are protected (dashboard/environment.py never reaches
    this path for them) and model vars aren't credential-slot-specific, so
    deleting a credential slot never needs to touch a model var. Returns
    None for any var that isn't an LLM-provider credential slot at all.
    """
    for family in _SLOTTED_FAMILIES:
        index = slot_index_for_var(family, var)
        if index is None:
            continue
        # A key_index override absent means slot 0 is the active default
        # (matches providers/key_index.py's own fallback) -- so deleting
        # slot 0 with no override present IS deleting the active slot.
        active_slot = key_index_overrides.get(family, 0)
        is_active_slot = active_slot == index
        return DeleteDependents(
            key_index_override=key_index_overrides.get(family) == index,
            # Only deactivate the provider if the slot actually being
            # deleted is the one currently in use -- deleting an unused
            # spare slot must not silently switch the bot off a provider
            # that was never depending on that slot in the first place.
            provider_override=(provider_override == family and is_active_slot),
        )
    return None


def conflicts_for(
    family: str, new_project_id: str | None, current_vertex_gcp_project: str | None
) -> list[dict[str, str]]:
    """Soft-dep mismatches a credential replacement should surface.

    Only vertex has a soft_dep whose correct value is derivable from the
    credential itself (VERTEX_GCP_PROJECT, embedded as `project_id` in the
    service-account JSON) -- VERTEX_GCP_LOCATION has no such embedded counterpart to
    compare against, so it's never flagged here, matching the design's
    "left untouched with a non-blocking note" decision.
    """
    if family != "vertex":
        return []
    if not current_vertex_gcp_project or not new_project_id:
        return []
    if current_vertex_gcp_project == new_project_id:
        return []
    return [{"var": "VERTEX_GCP_PROJECT", "current": current_vertex_gcp_project, "new": new_project_id}]
