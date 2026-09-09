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
# family's live model picker writes (LLM providers only). "derived": a var
# that is NEVER operator-authored, always recomputed from the credential --
# GitHub App's installation id.
CREDENTIAL_FAMILIES: dict[str, dict] = {
    "gemini": {"credential": ["GEMINI_API_KEY"], "model": "GEMINI_MODEL"},
    "groq": {"credential": ["GROQ_API_KEY"], "model": "GROQ_MODEL"},
    "vertex": {
        "credential": ["VERTEX_GCP_SERVICE_ACCOUNT_KEY"],
        "model": "VERTEX_MODEL",
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
    slot_config: bool = False

    def labels(self) -> list[str]:
        labels = []
        if self.key_index_override:
            labels.append("key_index override")
        if self.provider_override:
            labels.append("active provider override")
        if self.slot_config:
            labels.append("slotted model/project/location config")
        return labels

    def any(self) -> bool:
        return self.key_index_override or self.provider_override or self.slot_config


def dependents_of(
    var: str,
    *,
    key_index_overrides: dict[str, int],
    provider_override: str | None,
    slot_config_row: dict | None = None,
) -> DeleteDependents | None:
    """What runtime_config/slot_config state would dangle if `var` were deleted.

    Only LLM-provider credential slots have anything to compute: github_app's
    credential vars are protected (dashboard/environment.py never reaches
    this path for them). Returns None for any var that isn't an LLM-provider
    credential slot at all.

    `slot_config_row` is the caller's already-fetched
    store.get_slot_config(family, index) result (this module stays pure/I/O
    -free per its own module docstring) -- a non-None row means that slot has
    durable model/project/location config that would otherwise dangle as a
    "ghost" if a new credential later lands in the same slot number.
    Independent of whether the slot is currently active (unlike
    key_index_override/provider_override below): a spare, inactive slot can
    still carry a leftover slot_config row from when it was last configured.
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
            slot_config=slot_config_row is not None,
        )
    return None
