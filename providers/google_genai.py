"""The ``google-genai`` client adapter for Gemini (AI-Studio).

Per SPEC.md section 4 this calls ``client.aio.models.generate_content(...)``
with a JSON-schema response config, then parses+validates the raw text against
``schema`` locally (rather than trusting the SDK's own ``response.parsed``
field) so that ``validate.py``'s repair-retry logic has one single,
provider-agnostic notion of "validation failed" that doesn't depend on
SDK-internal behavior.

This file holds BOTH google-genai client shapes, as SPEC.md section 4 always
described it: ``vertex`` (``vertexai=True``, a GCP service-account identity)
and ``gemini`` (an AI-Studio ``api_key``) -- one SDK, two clients, one shared
``_complete()``. The vertex adapter was removed once (Vertex AI required an
attached payment card, which this project's no-card constraint ruled out) and
reinstated on 2026-08-14 when GCP billing/ADC access became available; see
CLAUDE.md's "Substitutions from the brief".
"""

from __future__ import annotations

from google import genai
from google.genai import types
from google.oauth2 import service_account
from pydantic import BaseModel

from config import settings
from providers.base import LLMResponse, parse_or_none, translate_rate_limit


async def _complete(
    client: genai.Client, model: str, system: str, user: str, schema: type[BaseModel]
) -> LLMResponse:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )
    async with translate_rate_limit(default=settings.dispatcher_default_retry_after_seconds):
        response = await client.aio.models.generate_content(
            model=model, contents=user, config=config
        )

    raw_text = response.text or ""
    usage = response.usage_metadata
    tokens_in = (usage.prompt_token_count or 0) if usage else 0
    # candidates_token_count alone excludes thoughts_token_count, but Google
    # bills thinking tokens at the output rate -- both gemini-flash-latest
    # and gemini-2.5-flash think by default, so omitting this silently
    # under-reports cost and under-counts against the usage cap. See
    # GenerateContentResponseUsageMetadata.total_token_count's own docstring
    # (sum of prompt + candidates + tool_use_prompt + thoughts).
    tokens_out = (
        (usage.candidates_token_count or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
        if usage
        else 0
    )

    return LLMResponse(
        raw_text=raw_text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        parsed=parse_or_none(raw_text, schema),
    )


class GeminiProvider:
    """``gemini`` (AI-Studio) — the actually-live provider in this environment."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        # Passed in, never read from Settings here: providers/active_model.py
        # is the single resolver, so a DB model override and the model reported
        # in the PR comment can never disagree with what actually runs.
        self._model = model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)


_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class VertexProvider:
    """``vertex`` -- gemini-flash-latest via Vertex AI (``vertexai=True``).

    Differs from GeminiProvider only in authentication: a GCP service-account
    identity instead of an API key. ``service_account_info=None`` means "pass
    no explicit credentials", which is what makes google-auth discover the
    caller's implicit ADC (``gcloud auth application-default login``).
    ``credentials=None`` is genai.Client's own default, so passing it
    explicitly here is identical to omitting it.

    Reinstated once GCP billing/ADC access became available -- see CLAUDE.md's
    "Substitutions from the brief".
    """

    def __init__(
        self, project: str, location: str, service_account_info: dict | None, model: str
    ) -> None:
        creds = None
        if service_account_info is not None:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=_VERTEX_SCOPES
            )
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        # Passed in, never read from Settings here: providers/active_model.py
        # is the single resolver, so a DB model override and the model reported
        # in the PR comment can never disagree with what actually runs.
        self._model = model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)
