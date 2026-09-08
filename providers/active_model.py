"""The model name actually in force per provider: a DB override when set, else
the env-configured value named by registry.PROVIDERS.

Every read of the active model goes through active_model(). Mirrors
providers/key_index.py exactly, including the reason for the split: the DB
read lives in the dispatcher (where the asyncio.to_thread convention applies)
and is pushed in via set_override_cache, keeping this module import-light and
non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails -- every provider degrades to its env model
rather than to a crash or an empty string. An empty cached value (hand-edited
row, or a future bug) is treated as "no override" for the same reason: an empty
model name is not a model, and sending one to a provider SDK is a guaranteed
failure where the env value is a working default. The env-model lookup itself
is guarded the same way: it can never return an empty string either, even
though every registry model var maps to a real, non-empty-by-default Settings
field today, so nothing currently exercises that fallback -- this value is
handed straight to a live LLM provider SDK, not just formatted into a PR
comment, so a defensive empty-string guard belongs here regardless.
"""

from __future__ import annotations

from config import settings
from providers import registry

_overrides: dict[str, str] = {}


def active_model(provider: str) -> str:
    """The model for `provider` -- its DB override when set and non-empty,
    else the env value named by registry.PROVIDERS.

    An unknown provider falls back to the gemini model rather than raising:
    callers include the PR-comment reporting path, which must never be able to
    abort a review.
    """
    override = _overrides.get(provider)
    if override:
        return override
    entry = registry.PROVIDERS.get(provider)
    if entry is None:
        return settings.gemini_model
    return getattr(settings, entry[1].lower(), "") or settings.gemini_model


def set_override_cache(overrides: dict[str, str]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
