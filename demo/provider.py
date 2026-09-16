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
from providers.base import LLMResponse

# Only pairs priced in providers/pricing.py; an unpriced pair renders a blank
# cost in the comment.
_PRICED = {
    "gemini": "gemini-flash-latest",
    "vertex": "gemini-flash-latest",
    "groq": "llama-3.3-70b-versatile",
}
_DEFAULT_PROVIDER = "groq"


def demo_provider_and_model(requested: str | None) -> tuple[str, str]:
    """Resolve a reader's provider choice to a priced (provider, model) pair."""
    provider = requested if requested in _PRICED else _DEFAULT_PROVIDER
    return provider, _PRICED[provider]


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
