"""Adapters convert a 429 transport error into RateLimited(retry_after).

Uses a lightweight fake exception (status_code/code + response.headers) rather
than constructing real SDK error objects, so the test is SDK-agnostic and makes
no network call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from config import settings
from providers.base import RateLimited, parse_retry_after
from providers.groq import GroqProvider


class Greeting(BaseModel):
    message: str


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeRateLimitError(Exception):
    def __init__(self, retry_after_header: str | None):
        super().__init__("429 rate limited")
        self.status_code = 429
        self.response = SimpleNamespace(
            headers={"retry-after": retry_after_header} if retry_after_header is not None else {}
        )


def _groq_raising(exc: Exception, monkeypatch):
    create = AsyncMock(side_effect=exc)
    monkeypatch.setattr(
        "providers.groq.AsyncGroq",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )


def test_parse_retry_after_seconds():
    assert parse_retry_after("30", NOW, default=60) == 30.0


def test_parse_retry_after_http_date():
    # 2026-01-01 12:02:00 GMT is 120 seconds after NOW.
    assert parse_retry_after("Thu, 01 Jan 2026 12:02:00 GMT", NOW, default=60) == 120.0


def test_parse_retry_after_missing_uses_default():
    assert parse_retry_after(None, NOW, default=60) == 60.0


async def test_groq_429_with_header_raises_rate_limited(monkeypatch):
    _groq_raising(FakeRateLimitError("30"), monkeypatch)
    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    with pytest.raises(RateLimited) as ei:
        await provider.complete(
            "s", "u", Greeting, timeout_seconds=45.0, default_retry_after_seconds=60.0
        )
    assert ei.value.retry_after == 30.0


async def test_groq_429_without_header_uses_default(monkeypatch):
    _groq_raising(FakeRateLimitError(None), monkeypatch)
    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    with pytest.raises(RateLimited) as ei:
        await provider.complete(
            "s", "u", Greeting, timeout_seconds=45.0, default_retry_after_seconds=60.0
        )
    assert ei.value.retry_after == 60.0


async def test_groq_non_429_error_propagates_unchanged(monkeypatch):
    _groq_raising(RuntimeError("network down"), monkeypatch)
    provider = GroqProvider(api_key="dummy-key-for-construction-only", model=settings.groq_model)
    with pytest.raises(RuntimeError):
        await provider.complete(
            "s", "u", Greeting, timeout_seconds=45.0, default_retry_after_seconds=60.0
        )
