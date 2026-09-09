"""Golden-ish tests for formatting.py — ReviewResult -> Markdown comment."""

from __future__ import annotations

from formatting import format_comment
from github_app import COMMENT_MARKER
from specialists.schemas import ReviewResult, SpecialistResult


def test_format_comment_includes_marker_first():
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=0.0001,
    )
    body = format_comment(result)
    assert body.startswith(COMMENT_MARKER)


def test_format_comment_surfaces_diff_truncation():
    """SPEC.md requires truncation to be visible in the comment, not just
    seen by the model via TRUNCATION_MARKER -- a human reading the PR must
    know most of the diff was never reviewed."""
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
        diff_truncated=True,
    )
    body = format_comment(result)
    assert "truncated" in body.lower()


def test_format_comment_omits_truncation_notice_when_not_truncated():
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
    )
    body = format_comment(result)
    assert "truncated" not in body.lower()


def test_format_comment_renders_findings_table():
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[
                    {
                        "severity": "critical",
                        "file": "app.py",
                        "line": 14,
                        "description": "Hardcoded API key",
                        "fix": "Move to env var",
                    }
                ],
                elapsed_ms=1200,
                tokens_in=100,
                tokens_out=50,
            )
        ],
        total_elapsed_ms=1200,
        total_tokens_in=100,
        total_tokens_out=50,
        est_cost_usd=0.0021,
    )
    body = format_comment(result)

    assert "groq" in body
    assert "llama-3.3-70b-versatile" in body
    assert "Security" in body
    assert "app.py:14" in body
    assert "Hardcoded API key" in body
    assert "Move to env var" in body
    assert "critical" in body


def test_format_comment_header_has_no_robot_emoji_or_pr_number():
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=0.0001,
    )
    body = format_comment(result)
    assert "## Automated Code Review\n" in body
    assert "🤖" not in body
    assert "PR #" not in body


def test_format_comment_section_headers_have_no_emoji_or_finding_count():
    result = ReviewResult(
        pr_number=1,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[
                    {
                        "severity": "critical",
                        "file": "app.py",
                        "line": 14,
                        "description": "Hardcoded API key",
                        "fix": "Move to env var",
                    }
                ],
                elapsed_ms=100,
            )
        ],
        total_elapsed_ms=100,
        total_tokens_in=1,
        total_tokens_out=1,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    assert "### Security\n" in body
    assert "🔒" not in body
    assert "finding" not in body.split("### Security\n", 1)[1].split("\n", 1)[0]


def test_format_comment_summary_table_lists_each_specialists_result():
    result = ReviewResult(
        pr_number=1,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[
                    {
                        "severity": "critical",
                        "file": "app.py",
                        "line": 14,
                        "description": "Hardcoded API key",
                        "fix": "Move to env var",
                    },
                    {
                        "severity": "high",
                        "file": "db.py",
                        "line": 44,
                        "description": "SQL via f-string",
                        "fix": "Parameterize",
                    },
                ],
                elapsed_ms=100,
            ),
            SpecialistResult(name="Performance", status="ok", findings=[], elapsed_ms=50),
            SpecialistResult(
                name="Code Quality", status="failed", findings=[], error="boom", elapsed_ms=10
            ),
        ],
        total_elapsed_ms=160,
        total_tokens_in=1,
        total_tokens_out=1,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    summary = body.split("### Security", 1)[0]
    assert "| Specialist | Result |" in summary
    assert "| Security | ❌ 2 findings |" in summary
    assert "| Performance | ✅ no findings |" in summary
    assert "| Code Quality | ❌ check failed |" in summary


def test_format_comment_renders_no_findings():
    result = ReviewResult(
        pr_number=7,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=500)],
        total_elapsed_ms=500,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=0.00005,
    )
    body = format_comment(result)
    assert "no findings" in body.lower()


def test_format_comment_renders_failed_specialist_visibly():
    result = ReviewResult(
        pr_number=7,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="failed",
                findings=[],
                error="DeadlineExceeded",
                elapsed_ms=500,
            )
        ],
        total_elapsed_ms=500,
        total_tokens_in=0,
        total_tokens_out=0,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    assert "failed" in body.lower()
    assert "DeadlineExceeded" in body


def test_format_comment_escapes_a_failed_specialists_raw_error_text():
    """format_failure's own docstring says failure sections never show raw
    exception text -- _render_section's failed-status branch must escape
    spec.error the same way every other cell is escaped, both so a stray `|`
    or newline in the error can't break the Markdown table/inject a header,
    and so a validation error that happened to echo secret material isn't
    rendered verbatim into a public PR comment."""
    result = ReviewResult(
        pr_number=7,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="failed",
                findings=[],
                error="boom | fake col ```\n### Injected header",
                elapsed_ms=500,
            )
        ],
        total_elapsed_ms=500,
        total_tokens_in=0,
        total_tokens_out=0,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    assert "boom \\| fake col" in body
    assert "```" not in body
    assert "\n### Injected header" not in body
    assert "boom" in body


def test_format_comment_escapes_pipe_and_newline_in_finding_text():
    """A crafted finding (attacker-controlled via the PR diff) must not be able
    to inject extra Markdown table columns/rows via `|` or a newline."""
    result = ReviewResult(
        pr_number=99,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[
                    {
                        "severity": "high",
                        "file": "app.py",
                        "line": 1,
                        "description": "Legit issue | fake col ```\n### Injected header",
                        "fix": "Escape it",
                    }
                ],
                elapsed_ms=100,
            )
        ],
        total_elapsed_ms=100,
        total_tokens_in=1,
        total_tokens_out=1,
        est_cost_usd=0.0,
    )
    body = format_comment(result)

    # The raw pipe/newline must not survive unescaped inside the table.
    assert "fake col ```\n### Injected header" not in body
    assert "\\|" in body
    # The newline is gone, so "### Injected header" can no longer start a new
    # line and render as a real Markdown heading -- it's now inert cell text.
    assert "\n### Injected header" not in body


def test_format_comment_does_not_hardcode_specialist_count():
    result = ReviewResult(
        pr_number=1,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=1,
        total_tokens_out=1,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    assert "3 specialists" not in body
    assert "1 specialist" in body


def _review_result(est_cost_usd: float | None) -> ReviewResult:
    return ReviewResult(
        pr_number=1,
        provider="groq",
        model="llama-3.1-8b-instant",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=est_cost_usd,
    )


def test_comment_omits_the_cost_when_the_model_is_unpriced():
    result = _review_result(est_cost_usd=None)
    body = format_comment(result)
    assert "$" not in body
    assert "tok in" in body and "tok out" in body
    assert "provider:" in body


def test_comment_still_shows_the_cost_when_the_model_is_priced():
    body = format_comment(_review_result(est_cost_usd=0.0004))
    assert "~$0.0004" in body
    assert "est. $0.0004" in body


def test_comment_shows_a_zero_cost_rather_than_omitting_it():
    """0.0 is falsy but PRESENT: a genuinely-priced review that happened to
    round to zero must still render its cost, unlike an unpriced one. Pins
    format_comment's `is not None` guard against a regression to a plain
    truthiness check, which would silently conflate the two."""
    body = format_comment(_review_result(est_cost_usd=0.0))
    assert "~$0.0000" in body
    assert "est. $0.0000" in body
