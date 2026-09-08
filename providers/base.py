"""Provider protocol + the uniform response shape every adapter returns.

Design decision (load-bearing for later steps — specialists.py, orchestrator.py):
``complete()`` does NOT return a bare validated Pydantic model. It returns
``LLMResponse``, which carries the model's raw text, real token usage, and a
best-effort ``parsed`` field.

Why: ``validate.py`` needs to know whether the raw output failed schema
validation so it can issue a single repair retry — and it needs BOTH calls'
token usage even when the first attempt fails (a wasted call still costs
tokens). If ``complete()`` only ever returned ``BaseModel | None`` there
would be nowhere to carry usage on a failed attempt. So each provider itself
attempts to parse+validate ``raw_text`` against ``schema`` (never raising on
a *validation* failure — only on a genuine transport/API error) and reports
the outcome via ``.parsed`` (``None`` on failure).

Later steps depend on this shape:
- ``validate.py`` composes one or two ``LLMResponse``s into a single
  ``ValidatedResult`` (ok / parsed / tokens_in / tokens_out / error).
- ``specialists/base.py`` (a later step) will read tokens_in/tokens_out off
  whatever ``validate.py`` returns to populate ``SpecialistResult``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

from pydantic import BaseModel, ValidationError


@dataclass
class LLMResponse:
    """Uniform provider return value.

    ``parsed`` is ``None`` when ``raw_text`` failed to parse as JSON or
    failed schema validation — providers never raise for that case, only
    for genuine transport/API errors.
    """

    raw_text: str
    tokens_in: int
    tokens_out: int
    parsed: BaseModel | None = None


class LLMProvider(Protocol):
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse: ...


KNOWN_PROVIDERS = ("gemini", "groq", "vertex")


class RateLimited(Exception):
    """Raised by an adapter when the provider returns HTTP 429.

    ``retry_after`` is seconds until a retry is allowed, taken from the
    provider's ``Retry-After`` header (or ``DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`` when
    the header is absent/unparseable). It is the SINGLE quota signal the
    dispatcher understands — a short value means a per-minute limit, a long
    value means a daily limit; the code does not distinguish them.
    """

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


def parse_retry_after(value: str | None, now: datetime, default: float) -> float:
    """Parse a ``Retry-After`` header value (delta-seconds or HTTP-date)."""
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - now).total_seconds())
    except (TypeError, ValueError):
        pass
    return default


def rate_limited_or_none(exc: Exception, now: datetime, default: float) -> "RateLimited | None":
    """Return a ``RateLimited`` if ``exc`` is a 429 transport error, else None.

    SDK-agnostic: OpenAI/Groq errors expose ``.status_code``; google-genai's
    ``APIError`` exposes ``.code``. Headers (if any) live on ``.response.headers``.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status != 429:
        return None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    retry_after = parse_retry_after(headers.get("retry-after"), now, default)
    return RateLimited(retry_after)


def parse_or_none(raw_text: str, schema: type[BaseModel]) -> BaseModel | None:
    """Best-effort JSON parse + schema validation. Never raises."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


@asynccontextmanager
async def translate_rate_limit(default: float):
    """Re-raise a 429 transport error as ``RateLimited``; anything else propagates.

    Wraps a single provider SDK call so every adapter shares one 429-detection
    path instead of duplicating the same try/except.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 -- re-raised as RateLimited or re-raised as-is below
        rl = rate_limited_or_none(exc, now=datetime.now(timezone.utc), default=default)
        if rl is not None:
            raise rl from exc
        raise
