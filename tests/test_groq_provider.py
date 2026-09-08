"""Deterministic tests for providers/groq.py.

Mocks the actual `groq.AsyncGroq` client boundary (`client.chat.completions.create`)
so nothing here ever makes a real network call. Live verification against the
real Groq API happens separately in scripts/manual_verify_groq.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from config import settings
from providers.groq import GroqProvider


class Greeting(BaseModel):
    message: str


def _fake_response(content: str, prompt_tokens: int = 10, completion_tokens: int = 5):
    """Build a stand-in for groq's ChatCompletion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


@pytest.mark.asyncio
async def test_groq_provider_parses_valid_structured_output(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 42, 7))
    monkeypatch.setattr(
        "providers.groq.AsyncGroq",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    result = await provider.complete(
        "system prompt",
        "user prompt",
        Greeting,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    fake_create.assert_awaited_once()
    _, kwargs = fake_create.call_args
    assert kwargs["model"] == settings.groq_model
    assert kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_groq_provider_returns_none_parsed_on_malformed_json(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response("not json at all", 10, 1))
    monkeypatch.setattr(
        "providers.groq.AsyncGroq",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    result = await provider.complete(
        "system prompt",
        "user prompt",
        Greeting,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.parsed is None
    assert result.tokens_in == 10
    assert result.tokens_out == 1


@pytest.mark.asyncio
async def test_groq_provider_returns_none_parsed_on_off_schema_json(monkeypatch):
    fake_create = AsyncMock(
        return_value=_fake_response(json.dumps({"totally": "wrong shape"}), 10, 1)
    )
    monkeypatch.setattr(
        "providers.groq.AsyncGroq",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    result = await provider.complete(
        "system prompt",
        "user prompt",
        Greeting,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.parsed is None


@pytest.mark.asyncio
async def test_groq_provider_includes_schema_in_system_prompt(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"})))
    monkeypatch.setattr(
        "providers.groq.AsyncGroq",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    await provider.complete(
        "Be a helpful reviewer.",
        "user prompt",
        Greeting,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    _, kwargs = fake_create.call_args
    system_message = kwargs["messages"][0]
    assert system_message["role"] == "system"
    assert "Be a helpful reviewer." in system_message["content"]
    assert "message" in system_message["content"]  # schema property name present
    user_message = kwargs["messages"][1]
    assert user_message == {"role": "user", "content": "user prompt"}
