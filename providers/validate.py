"""Validate-and-repair layer sitting above any ``LLMProvider``.

Failure signal (load-bearing for the orchestrator, a later step): this
module never raises on a model-output problem — it always returns a
``ValidatedResult`` with ``.ok``. The orchestrator/specialists check ``.ok``
and render a "specialist failed" row (SPEC.md section 5/6) instead of
crashing the whole review. ``.tokens_in``/``.tokens_out`` accumulate across
the original attempt AND the repair retry (if one happened), so cost
accounting stays accurate even when the first attempt produced unusable
output.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from providers.base import LLMProvider

REPAIR_INSTRUCTION = (
    "\n\nReturn ONLY valid JSON matching the given schema. No prose, "
    "no markdown code fences, no other text."
)


@dataclass
class ValidatedResult:
    ok: bool
    parsed: BaseModel | None
    tokens_in: int
    tokens_out: int
    error: str | None = None


async def validate_and_repair(
    provider: LLMProvider,
    system: str,
    user: str,
    schema: type[BaseModel],
    *,
    timeout_seconds: float,
    default_retry_after_seconds: float,
) -> ValidatedResult:
    """Call ``provider``, validating structured output with one repair retry.

    1. Call the provider. If the output already validated against ``schema``,
       return success.
    2. Otherwise, retry exactly once with a repair instruction appended to
       the system prompt.
    3. If the repair retry also fails validation, return a failed
       ``ValidatedResult`` (never raises) — the caller renders this as a
       failed specialist rather than crashing.

    ``timeout_seconds``/``default_retry_after_seconds`` are threaded straight
    through to both calls -- see ``LLMProvider.complete()``'s docstring for
    why these are parameters rather than something a provider reads itself.
    """
    first = await provider.complete(
        system,
        user,
        schema,
        timeout_seconds=timeout_seconds,
        default_retry_after_seconds=default_retry_after_seconds,
    )
    if first.parsed is not None:
        return ValidatedResult(
            ok=True, parsed=first.parsed, tokens_in=first.tokens_in, tokens_out=first.tokens_out
        )

    repaired_system = system + REPAIR_INSTRUCTION
    second = await provider.complete(
        repaired_system,
        user,
        schema,
        timeout_seconds=timeout_seconds,
        default_retry_after_seconds=default_retry_after_seconds,
    )
    tokens_in = first.tokens_in + second.tokens_in
    tokens_out = first.tokens_out + second.tokens_out

    if second.parsed is not None:
        return ValidatedResult(
            ok=True, parsed=second.parsed, tokens_in=tokens_in, tokens_out=tokens_out
        )

    return ValidatedResult(
        ok=False,
        parsed=None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        error="model output failed schema validation after one repair retry",
    )
