"""Tests for specialists/quality.py."""

from __future__ import annotations

from specialists.quality import (
    QUALITY_SYSTEM_PROMPT,
    QualityFindings,
    run_quality_specialist,
)
from specialists.schemas import QualityFinding


def test_quality_findings_container_wraps_list_of_quality_finding():
    container = QualityFindings(
        findings=[
            QualityFinding(
                category="magic-number",
                file="app.py",
                line=57,
                issue="Unexplained threshold literal",
                refactoring_suggestion="Extract to a named constant",
            )
        ]
    )
    assert len(container.findings) == 1
    assert container.findings[0].category == "magic-number"


def test_quality_system_prompt_mentions_key_categories():
    prompt_lower = QUALITY_SYSTEM_PROMPT.lower()
    for keyword in ("duplication", "naming", "magic number"):
        assert keyword in prompt_lower


async def test_run_quality_specialist_success(monkeypatch):
    from providers.base import LLMResponse

    parsed = QualityFindings(
        findings=[
            QualityFinding(
                category="magic-number",
                file="app.py",
                line=57,
                issue="Unexplained threshold literal",
                refactoring_suggestion="Extract to a named constant",
            )
        ]
    )

    class FakeProvider:
        async def complete(self, system, user, schema, **kwargs):
            assert schema is QualityFindings
            return LLMResponse(raw_text="{}", tokens_in=15, tokens_out=8, parsed=parsed)

    monkeypatch.setattr("specialists.base.get_provider", lambda: FakeProvider())

    result = await run_quality_specialist(
        "annotated diff text",
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.name == "Code Quality"
    assert result.status == "ok"
    assert result.findings[0]["category"] == "magic-number"
    assert result.tokens_in == 15
    assert result.tokens_out == 8
