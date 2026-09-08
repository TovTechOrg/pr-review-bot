"""``groq`` provider adapter — OpenAI-compatible API, Llama model, cross-vendor.

Per SPEC.md section 4: a different vendor + model (Llama, not Gemini)
demonstrates true provider-agnosticism. Implements ``LLMProvider.complete()``.

Structured output: the model actually available and installed
(``llama-3.3-70b-versatile``, chosen per CLAUDE.md task 1) does NOT support
Groq's ``response_format={"type": "json_schema"}`` (constrained decoding) —
verified live, it returns HTTP 400 "This model does not support response
format `json_schema`". Only a smaller set of Groq-hosted models (mostly
``openai/gpt-oss-*``) support that. So this adapter uses the older
``{"type": "json_object"}`` mode (guarantees valid JSON syntax, but not schema
conformance) plus a schema-instructing system prompt — same approach called
out as a fallback in the task brief. As with ``google_genai.py``, raw text is
parsed+validated against ``schema`` locally (never trusting vendor-side
guarantees), so ``validate.py``'s repair-retry logic has one single,
provider-agnostic notion of "validation failed".
"""

from __future__ import annotations

import json

from groq import AsyncGroq
from pydantic import BaseModel

from config import settings
from providers.base import LLMResponse, parse_or_none, translate_rate_limit


def _schema_system_prompt(system: str, schema: type[BaseModel]) -> str:
    """Append the JSON schema to the system prompt.

    ``json_object`` mode only guarantees syntactically-valid JSON, not
    conformance to any particular shape — the model needs the schema spelled
    out in the prompt to have a chance of matching it.
    """
    schema_json = json.dumps(schema.model_json_schema())
    return (
        f"{system}\n\nRespond ONLY with valid JSON matching this JSON schema, "
        f"no prose, no markdown code fences:\n{schema_json}"
    )


class GroqProvider:
    """``groq`` — cross-vendor fallback (Llama via Groq's OpenAI-compatible API)."""

    def __init__(self, api_key: str, model: str) -> None:
        # max_retries=0: the SDK's own default (2) silently retries a 429
        # with backoff before this adapter's except clause ever sees it --
        # confirmed live (a 43.1s call, vs. ~5s normal, that never surfaced
        # as RateLimited despite exceeding the account's TPM budget). The
        # durable queue (review_queue/dispatcher.py) already owns retry/backoff
        # for a rate-limited review -- durable across a process restart,
        # visible via a placeholder/schedule-note comment -- so a second,
        # hidden retry layer underneath it is redundant at best and actively
        # hides a real signal at worst.
        self._client = AsyncGroq(
            api_key=api_key,
            max_retries=0,
            timeout=settings.llm_request_timeout_seconds,
        )
        # Passed in, never read from Settings here: providers/active_model.py
        # is the single resolver, so a DB model override and the model reported
        # in the PR comment can never disagree with what actually runs.
        self._model = model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        async with translate_rate_limit(default=settings.dispatcher_default_retry_after_seconds):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _schema_system_prompt(system, schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=parse_or_none(raw_text, schema),
        )
