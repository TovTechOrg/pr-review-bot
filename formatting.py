"""Render a ``ReviewResult`` into the Markdown PR comment (SPEC.md section 6).

Knows nothing about LLMs or providers — only how to turn the already-computed
``ReviewResult`` envelope into Markdown. Each specialist's findings are
``list[dict]`` (see specialists/schemas.py), so this module maps specialist
name -> column layout to render each finding's fields as a table row.

Generalization note: this step's ``ReviewResult.results`` has exactly one
entry (Security). The section-count and specialist-count language below is
computed from ``len(result.results)``, not hardcoded to "3 specialists" or
"1 specialist" — step 6 adds Performance + Code Quality without touching
this file's control flow, only the ``_SECTION_CONFIG`` table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from github_app import (
    COMMENT_MARKER,
    FAIL_NOTE_END,
    FAIL_NOTE_START,
    SCHEDULE_NOTE_END,
    SCHEDULE_NOTE_START,
)
from specialists.schemas import ReviewResult, SpecialistResult

_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡"}

PLACEHOLDER_DAILY_THRESHOLD_SECONDS = 300

# Per-specialist section rendering config: table columns.
# Each column is (dict-key-in-finding, header-label, optional formatter).
_SECTION_CONFIG: dict[str, dict] = {
    "Security": {
        "columns": [
            ("severity", "Severity", lambda v: f"{_SEVERITY_EMOJI.get(v, '')} {v}".strip()),
            ("_file_line", "Line", None),
            ("description", "Issue", None),
            ("fix", "Suggested fix", None),
        ],
    },
    "Performance": {
        "columns": [
            ("estimated_impact", "Impact", lambda v: f"{_SEVERITY_EMOJI.get(v, '')} {v}".strip()),
            ("_file_line", "Line", None),
            ("type", "Issue", None),
            ("suggestion", "Suggestion", None),
        ],
    },
    "Code Quality": {
        "columns": [
            ("category", "Category", None),
            ("_file_line", "Line", None),
            ("issue", "Issue", None),
            ("refactoring_suggestion", "Refactoring suggestion", None),
        ],
    },
}


def _escape_cell(value: object) -> str:
    """Neutralize Markdown table syntax in LLM-generated, PR-diff-derived text."""
    text = str(value)
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _file_line(finding: dict) -> str:
    return f"`{_escape_cell(finding.get('file', '?'))}:{finding.get('line', '?')}`"


def _summary_row(spec: SpecialistResult) -> str:
    if spec.status == "failed":
        result = "❌ check failed"
    elif not spec.findings:
        result = "✅ no findings"
    else:
        plural = "s" if len(spec.findings) != 1 else ""
        result = f"❌ {len(spec.findings)} finding{plural}"
    return f"| {spec.name} | {result} |"


def _render_summary_table(results: list[SpecialistResult]) -> str:
    rows = "\n".join(_summary_row(spec) for spec in results)
    return f"| Specialist | Result |\n| --- | --- |\n{rows}\n"


def _render_section(spec: SpecialistResult) -> str:
    config = _SECTION_CONFIG.get(spec.name, {"columns": []})

    if spec.status == "failed":
        return (
            f"### {spec.name}\n"
            f"❌ check failed\n"
            f"> `{_escape_cell(spec.error)}` — other checks completed normally.\n"
        )

    if not spec.findings:
        return f"### {spec.name}\n✅ no findings\n"

    header = f"### {spec.name}\n"
    columns = config["columns"]
    col_headers = " | ".join(label for _, label, _ in columns)
    col_sep = " | ".join("---" for _ in columns)
    rows = []
    for finding in spec.findings:
        cells = []
        for key, _label, fmt in columns:
            if key == "_file_line":
                cells.append(_file_line(finding))
                continue
            raw = finding.get(key, "")
            value = fmt(raw) if fmt else raw
            cells.append(_escape_cell(value))
        rows.append("| " + " | ".join(cells) + " |")

    table = f"| {col_headers} |\n| {col_sep} |\n" + "\n".join(rows)
    return header + table + "\n"


def format_comment(result: ReviewResult) -> str:
    """Render ``result`` into the marker-prefixed Markdown PR comment body."""
    n = len(result.results)
    plural = "specialist" if n == 1 else "specialists"
    runtime_s = result.total_elapsed_ms / 1000
    cost_str = f" · ~${result.est_cost_usd:.4f}" if result.est_cost_usd is not None else ""
    cost_footer = (
        f"est. ${result.est_cost_usd:.4f} · " if result.est_cost_usd is not None else ""
    )

    header = (
        f"## Automated Code Review\n"
        f"_{n} {plural} · {result.model} ({result.provider}) · {runtime_s:.1f}s{cost_str}_\n"
    )
    if result.diff_truncated:
        header += (
            "\n⚠️ This PR's diff exceeded the review token budget and was "
            "truncated — some changes may not have been reviewed.\n"
        )

    summary = _render_summary_table(result.results)
    sections = "\n".join(_render_section(spec) for spec in result.results)

    footer = (
        "\n---\n"
        f"<sub>Runtime {runtime_s:.1f}s · {result.total_tokens_in:,} tok in / "
        f"{result.total_tokens_out:,} tok out · {cost_footer}"
        f"provider: {result.provider}</sub>\n"
    )

    body = f"{header}\n{summary}\n{sections}{footer}"
    return f"{COMMENT_MARKER}\n{body}"


def format_placeholder(
    pr_number: int,
    retry_after: float,
    now: datetime,
    reason: Literal["provider", "usage_cap", "config"] = "provider",
) -> str:
    """Marker-prefixed placeholder comment shown while a review is delayed.

    The real result later edits this same comment in place (found via the
    marker). ``reason`` defaults to "provider", so every pre-existing call
    site renders byte-identically to before:

    - "provider": wording is chosen by wait magnitude -- short = per-minute
      rate limit; long = the provider's daily quota, with an ETA computed
      from ``now + retry_after``.
    - "usage_cap": the bot's OWN per-key daily cap. Always the same wording
      regardless of wait length (the cause doesn't change with magnitude),
      and explicit that this is not the provider's limit -- an operator
      debugging a stalled review must not go hunting at the provider.
    - "config": the dispatcher's own tuning config is missing or invalid
      (review_queue/dispatcher_tuning_config.py). Names the cause plainly so
      an operator doesn't mistake a stuck queue for a provider outage.
    """
    header = "## Automated Code Review\n"
    eta = (now + timedelta(seconds=retry_after)).strftime("%H:%M UTC")
    if reason == "config":
        note = (
            "⚠️ Dispatcher configuration issue — review queued, will retry "
            "automatically once an operator fixes the dispatcher's tuning "
            "config (see the dashboard's config panel)."
        )
    elif reason == "usage_cap":
        note = (
            "⏳ Bot's own daily usage limit reached for this key — review "
            "queued, will post automatically after the limit resets "
            f"(~{eta}). This is not a provider rate limit."
        )
    elif retry_after < PLACEHOLDER_DAILY_THRESHOLD_SECONDS:
        note = "⏳ Queued behind rate limit — review will appear shortly."
    else:
        note = (
            "⏳ Daily model quota reached — review queued, will post "
            f"automatically after the provider's limit resets (~{eta})."
        )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"


def format_failure(pr_number: int, attempts: int) -> str:
    """Marker-prefixed comment shown when a review is abandoned after repeated
    hard failures AND no prior good review exists to preserve. Shows only the
    attempt count — never raw exception text (secrets hygiene)."""
    header = "## Automated Code Review\n"
    plural = "attempt" if attempts == 1 else "attempts"
    note = (
        f"❌ Automated review could not be completed after {attempts} {plural} "
        "due to a service error. It will retry automatically on the next push."
    )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"


def format_failure_footnote(attempts: int) -> str:
    """FAIL_NOTE_*-delimited footnote appended below a preserved good review when a
    later re-review hard-fails. Self-cleaning (the next successful review overwrites
    the whole comment) and idempotent (replaces any prior footnote). No raw error text."""
    plural = "attempt" if attempts == 1 else "attempts"
    return (
        f"{FAIL_NOTE_START}\n"
        f"> ⚠️ A later automated re-review could not be completed after {attempts} "
        f"{plural} (service error). The review above may be behind the latest commit; "
        "it will retry on the next push.\n"
        f"{FAIL_NOTE_END}"
    )


def format_schedule_notice(
    not_before: datetime, reason: Literal["provider", "usage_cap"] = "provider"
) -> str:
    """Self-cleaning notice appended below a preserved good review when the
    next re-review is scheduled (cooldown, rate-limit wait, or the bot's own
    usage cap). Absolute UTC time only -- GitHub's comment body can't be
    localized per viewer, and this note is only edited on a re-arm event (not
    continuously updated), so a relative string would go stale the moment
    it's posted. Requires a timezone-aware ``not_before``:
    ``datetime.astimezone()`` silently treats a naive datetime as system-local
    time rather than raising, so a naive value is rejected explicitly here
    instead of producing a host-timezone-dependent result.

    ``reason`` defaults to "provider", preserving today's exact wording for
    every pre-existing call site.
    """
    if not_before.tzinfo is None:
        raise ValueError("format_schedule_notice requires a timezone-aware datetime")
    eta = not_before.astimezone(timezone.utc).strftime("%H:%M UTC")
    if reason == "usage_cap":
        body = (
            f"🔄 Re-review scheduled ~{eta} (usage limit reached — resets "
            "automatically, not a provider quota issue)"
        )
    else:
        body = f"🔄 Re-review scheduled ~{eta}"
    return f"{SCHEDULE_NOTE_START}\n{body}\n{SCHEDULE_NOTE_END}"
