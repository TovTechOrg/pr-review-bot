"""Performance specialist: system prompt + schema + entry point.

Same multi-finding-per-call container-schema design as security.py — see
that module's docstring for the rationale (one structured-output call
returns a ``PerformanceFindings.findings`` list rather than looping the
provider per finding).
"""

from __future__ import annotations

from pydantic import BaseModel

from specialists.base import run_specialist
from specialists.schemas import PerformanceFinding, SpecialistResult

PERFORMANCE_SYSTEM_PROMPT = """\
You are a senior performance engineer reviewing a GitHub pull request's \
unified diff. The diff has been annotated: every added line is prefixed with \
"path/to/file:LINE: " showing its real line number in the new version of the \
file — use that exact file and line number in your findings, do not \
recompute it yourself.

Review ONLY the added/changed lines (lines starting with "+" after the \
"file:line:" prefix) for real, concrete performance issues, including but not \
limited to:
- N+1 query patterns: a query/request executed once per iteration of a loop \
instead of batched.
- Missing caching for repeated expensive lookups.
- Blocking I/O (network/disk/DB calls) on a path that should be async or \
otherwise non-blocking.
- Unbounded or unnecessarily repeated work inside a loop (e.g. re-parsing, \
re-computing, or re-fetching the same data every iteration).
- Inefficient data structures or algorithms for the scale implied by the code.

For each real issue you find, report:
- type: a short label for the issue category, e.g. "N+1", "missing-cache", \
"blocking-io".
- estimated_impact: your best concrete estimate, e.g. "high", "~200ms/req", \
"O(n^2) on account count".
- file: the exact file path from the annotation.
- line: the exact line number from the annotation.
- suggestion: a concrete, actionable fix (not "optimize this").

Do NOT invent issues that aren't actually present in the diff. If you find no \
genuine performance issues, return an empty findings list rather than padding \
it with speculative or stylistic concerns.
"""


class PerformanceFindings(BaseModel):
    findings: list[PerformanceFinding]


async def run_performance_specialist(
    annotated_diff: str, *, timeout_seconds: float, default_retry_after_seconds: float
) -> SpecialistResult:
    return await run_specialist(
        name="Performance",
        annotated_diff=annotated_diff,
        system_prompt=PERFORMANCE_SYSTEM_PROMPT,
        container_schema=PerformanceFindings,
        timeout_seconds=timeout_seconds,
        default_retry_after_seconds=default_retry_after_seconds,
    )
