"""Security specialist: system prompt + schema + entry point.

Multi-finding-per-call design (see also specialists/base.py's module
docstring): ``SecurityFinding`` describes exactly one finding, but a
specialist call must return a *list* of findings. ``SecurityFindings`` wraps
it in a ``findings: list[SecurityFinding]`` container, which is what's
actually handed to the provider as the structured-output schema. This keeps
``SecurityFinding`` itself clean (matches SPEC.md section 3 exactly, no list
wrapper baked into the field-matching model) while still letting one LLM
call return every finding it sees in the diff.
"""

from __future__ import annotations

from pydantic import BaseModel

from specialists.base import run_specialist
from specialists.schemas import SecurityFinding, SpecialistResult

SECURITY_SYSTEM_PROMPT = """\
You are a senior application security engineer reviewing a GitHub pull \
request's unified diff. The diff has been annotated: every added line is \
prefixed with "path/to/file:LINE: " showing its real line number in the new \
version of the file — use that exact file and line number in your findings, \
do not recompute it yourself.

Review ONLY the added/changed lines (lines starting with "+" after the \
"file:line:" prefix) for real, concrete security issues, including but not \
limited to:
- Hardcoded credentials, API keys, tokens, or secrets committed as literals.
- Injection risks: SQL/command/template injection from unsanitized input \
reaching a query, shell call, or template render.
- Unsafe deserialization (pickle, yaml.load without SafeLoader, eval/exec \
on untrusted input).
- Missing authentication/authorization checks on sensitive operations.
- Insecure use of cryptography (weak hashes for passwords, hardcoded IVs/salts, \
disabled certificate verification).
- Path traversal / unsanitized file paths built from user input.

For each real issue you find, report:
- severity: "critical" | "high" | "medium" — critical for anything directly \
exploitable or a live secret; high for a clear vulnerability needing specific \
input; medium for a defense-in-depth or best-practice gap.
- file: the exact file path from the annotation.
- line: the exact line number from the annotation.
- description: a precise, one-to-two sentence explanation of the issue.
- fix: a concrete, actionable remediation (not "be more careful").

Do NOT invent issues that aren't actually present in the diff. If you find \
no genuine security issues, return an empty findings list rather than \
padding it with speculative or stylistic concerns — this is a security \
review, not a general code review.
"""


class SecurityFindings(BaseModel):
    findings: list[SecurityFinding]


async def run_security_specialist(
    annotated_diff: str, *, timeout_seconds: float, default_retry_after_seconds: float
) -> SpecialistResult:
    return await run_specialist(
        name="Security",
        annotated_diff=annotated_diff,
        system_prompt=SECURITY_SYSTEM_PROMPT,
        container_schema=SecurityFindings,
        timeout_seconds=timeout_seconds,
        default_retry_after_seconds=default_retry_after_seconds,
    )
