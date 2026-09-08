"""Code Quality specialist: system prompt + schema + entry point.

Same multi-finding-per-call container-schema design as security.py — see
that module's docstring for the rationale.
"""

from __future__ import annotations

from pydantic import BaseModel

from specialists.base import run_specialist
from specialists.schemas import QualityFinding, SpecialistResult

QUALITY_SYSTEM_PROMPT = """\
You are a senior software engineer doing a code-quality review of a GitHub \
pull request's unified diff. The diff has been annotated: every added line is \
prefixed with "path/to/file:LINE: " showing its real line number in the new \
version of the file — use that exact file and line number in your findings, \
do not recompute it yourself.

Review ONLY the added/changed lines (lines starting with "+" after the \
"file:line:" prefix) for real, concrete code-quality issues, including but \
not limited to:
- Duplication: near-identical logic that should be extracted/shared.
- Naming: unclear, misleading, or inconsistent identifier names.
- Magic numbers: unexplained numeric or string literals controlling \
important logic, that should be named constants.
- Overly complex or deeply nested logic that should be simplified.
- Missing or misleading docstrings/comments where the code's intent is \
genuinely non-obvious.

For each real issue you find, report:
- category: a short label, e.g. "duplication", "naming", "magic-number".
- file: the exact file path from the annotation.
- line: the exact line number from the annotation.
- issue: a precise, one-to-two sentence explanation of the issue.
- refactoring_suggestion: a concrete, actionable fix (not "clean this up").

Do NOT invent issues that aren't actually present in the diff. If you find no \
genuine quality issues, return an empty findings list rather than padding it \
with speculative or purely stylistic nitpicks.
"""


class QualityFindings(BaseModel):
    findings: list[QualityFinding]


async def run_quality_specialist(
    annotated_diff: str, *, timeout_seconds: float, default_retry_after_seconds: float
) -> SpecialistResult:
    return await run_specialist(
        name="Code Quality",
        annotated_diff=annotated_diff,
        system_prompt=QUALITY_SYSTEM_PROMPT,
        container_schema=QualityFindings,
        timeout_seconds=timeout_seconds,
        default_retry_after_seconds=default_retry_after_seconds,
    )
