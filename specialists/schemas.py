"""Pydantic finding models + envelopes shared by every specialist.

Per SPEC.md section 3 — field names match the brief exactly, plus a ``file``
field so the comment can render ``file:line`` without a second round trip.

All three specialists (``specialists/security.py``, ``performance.py``,
``quality.py``) are wired up and run concurrently via
``orchestrator.py``'s ``asyncio.gather``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["critical", "high", "medium"]


class SecurityFinding(BaseModel):
    severity: Severity
    file: str
    line: int
    description: str
    fix: str


class PerformanceFinding(BaseModel):
    type: str  # e.g. "N+1", "missing-cache", "blocking-io"
    estimated_impact: str  # e.g. "high", "~200ms/req"
    file: str
    line: int
    suggestion: str


class QualityFinding(BaseModel):
    category: str  # e.g. "duplication", "naming", "magic-number"
    file: str
    line: int
    issue: str
    refactoring_suggestion: str


class SpecialistResult(BaseModel):
    name: Literal["Security", "Performance", "Code Quality"]
    status: Literal["ok", "failed"]
    findings: list[dict] = []  # serialized findings of the specialist's type
    error: str | None = None
    elapsed_ms: int
    tokens_in: int = 0
    tokens_out: int = 0


class ReviewResult(BaseModel):
    pr_number: int
    provider: str  # active runtime_config.provider
    model: str
    results: list[SpecialistResult]
    total_elapsed_ms: int
    total_tokens_in: int
    total_tokens_out: int
    est_cost_usd: float | None = None
    # True when diff_utils.annotate_and_cap truncated the diff for exceeding
    # its token budget -- surfaced in the comment (formatting.py) per
    # SPEC.md's "visible truncation" requirement, not just seen by the model.
    diff_truncated: bool = False
