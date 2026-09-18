"""A provider adapter that answers with canned findings and calls nothing.

Satisfies the ``complete()`` Protocol in providers/base.py. It deliberately
does NOT introduce a new provider name: it answers *as* whichever real
provider the reader chose, so the review comment reports that choice and
pricing.py can price it.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from demo.content import FINDINGS_BY_SCHEMA
from demo.model_catalog import MODELS_BY_PROVIDER
from providers.base import LLMResponse

# The priced default per provider (providers/pricing.py); used whenever no
# model was requested, or the requested one doesn't belong to that
# provider's demo/model_catalog.py catalog. An unpriced pair (the catalog's
# second entry) renders a blank cost in the comment -- see
# demo/model_catalog.py's own docstring for why that's preferred over a
# fabricated rate.
_PRICED = {provider: models[0] for provider, models in MODELS_BY_PROVIDER.items()}
_DEFAULT_PROVIDER = "groq"


def demo_provider_and_model(
    requested_provider: str | None, requested_model: str | None = None
) -> tuple[str, str]:
    """Resolve a reader's provider/model choice to a (provider, model) pair.

    `requested_model` is honored only when it belongs to the RESOLVED
    provider's own catalog -- a model name that names another provider's
    model, or an unrecognized one, falls back to that provider's priced
    default rather than being reported/priced as something MockProvider
    never actually "ran".
    """
    provider = requested_provider if requested_provider in _PRICED else _DEFAULT_PROVIDER
    model = (
        requested_model
        if requested_model in MODELS_BY_PROVIDER[provider]
        else _PRICED[provider]
    )
    return provider, model


class MockProvider:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    async def complete(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        timeout_seconds: float,
        default_retry_after_seconds: float,
    ) -> LLMResponse:
        findings = FINDINGS_BY_SCHEMA.get(schema.__name__, [])
        raw_text = json.dumps({"findings": findings})
        return LLMResponse(
            raw_text=raw_text,
            tokens_in=len(user) // 4 or 1,
            tokens_out=len(raw_text) // 4 or 1,
            # Parsed on the first call so validate_and_repair never spends its
            # repair retry (providers/validate.py retries once when parsed is
            # None).
            parsed=schema(**{"findings": findings}),
        )
