"""Shared ``run()`` shape for every specialist.

Design decision (multi-finding-per-call): a single structured-output call
returns exactly one instance of the given schema — but SPEC.md's comment
format (section 6) expects a LIST of findings per specialist. Rather than
looping the provider per-finding (expensive, and the model doesn't know the
total finding count up front), each specialist wraps its per-finding schema
(e.g. ``SecurityFinding``) in a small **container schema** with a single
``findings: list[...]`` field (e.g. ``SecurityFindings``) and asks the model
for the container. ``validate_and_repair`` validates against the container
schema as a whole; this module then unwraps ``.findings`` and serializes
each finding to a plain dict for ``SpecialistResult.findings`` (which is
typed ``list[dict]`` precisely so it can hold any of the three finding
shapes without the envelope needing a union type).

Never raises: a specialist's job is to always produce a valid
``SpecialistResult`` — a bad LLM response becomes ``status="failed"`` with
``.error`` set, not an exception. This matters even solo (this step) and is
what makes step 6's ``asyncio.gather(..., return_exceptions=True)``
partial-failure handling trivial to add later.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from providers.base import RateLimited
from providers.factory import get_provider
from providers.validate import validate_and_repair
from specialists.schemas import SpecialistResult

SpecialistName = str  # narrowed to the Literal by SpecialistResult itself


async def run_specialist(
    *,
    name: SpecialistName,
    annotated_diff: str,
    system_prompt: str,
    container_schema: type[BaseModel],
    timeout_seconds: float,
    default_retry_after_seconds: float,
) -> SpecialistResult:
    """Run one specialist end-to-end: provider call -> validate-repair -> envelope.

    ``container_schema`` must have a single ``findings: list[...]`` field.
    Never raises — any provider/validation failure becomes a
    ``status="failed"`` ``SpecialistResult``.

    ``timeout_seconds``/``default_retry_after_seconds`` come from the caller
    (orchestrator.py, which reads review_queue.dispatcher_tuning_config's
    DB-refreshed cache) rather than being read here or inside providers/ --
    this keeps providers/ and specialists/ fully dependency-free from
    review_queue/, matching this project's existing layering (see
    providers/base.py::LLMProvider's docstring).
    """
    started = time.monotonic()
    tokens_in = 0
    tokens_out = 0

    try:
        provider = get_provider()
        validated = await validate_and_repair(
            provider,
            system_prompt,
            annotated_diff,
            container_schema,
            timeout_seconds=timeout_seconds,
            default_retry_after_seconds=default_retry_after_seconds,
        )
        tokens_in = validated.tokens_in
        tokens_out = validated.tokens_out

        if not validated.ok or validated.parsed is None:
            return SpecialistResult(
                name=name,
                status="failed",
                findings=[],
                error=validated.error or "specialist produced no usable output",
                elapsed_ms=_elapsed_ms(started),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        findings = [f.model_dump() for f in validated.parsed.findings]
        return SpecialistResult(
            name=name,
            status="ok",
            findings=findings,
            error=None,
            elapsed_ms=_elapsed_ms(started),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    except RateLimited:
        raise  # must reach the orchestrator so it can defer, not render a failed row
    # a specialist must never crash the orchestrator
    except Exception as exc:  # noqa: BLE001
        return SpecialistResult(
            name=name,
            status="failed",
            findings=[],
            error=str(exc),
            elapsed_ms=_elapsed_ms(started),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
