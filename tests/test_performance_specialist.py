"""Tests for specialists/performance.py."""

from __future__ import annotations

from specialists.performance import (
    PERFORMANCE_SYSTEM_PROMPT,
    PerformanceFindings,
    run_performance_specialist,
)
from specialists.schemas import PerformanceFinding


def test_performance_findings_container_wraps_list_of_performance_finding():
    container = PerformanceFindings(
        findings=[
            PerformanceFinding(
                type="N+1",
                estimated_impact="high",
                file="app.py",
                line=40,
                suggestion="Batch the query outside the loop",
            )
        ]
    )
    assert len(container.findings) == 1
    assert container.findings[0].type == "N+1"


def test_performance_system_prompt_mentions_key_risk_categories():
    prompt_lower = PERFORMANCE_SYSTEM_PROMPT.lower()
    for keyword in ("n+1", "blocking", "cache"):
        assert keyword in prompt_lower


async def test_run_performance_specialist_success(monkeypatch):
    from providers.base import LLMResponse

    parsed = PerformanceFindings(
        findings=[
            PerformanceFinding(
                type="N+1",
                estimated_impact="high",
                file="app.py",
                line=40,
                suggestion="Batch the query outside the loop",
            )
        ]
    )

    class FakeProvider:
        async def complete(self, system, user, schema, **kwargs):
            assert schema is PerformanceFindings
            return LLMResponse(raw_text="{}", tokens_in=18, tokens_out=9, parsed=parsed)

    monkeypatch.setattr("specialists.base.get_provider", lambda: FakeProvider())

    result = await run_performance_specialist(
        "annotated diff text",
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.name == "Performance"
    assert result.status == "ok"
    assert result.findings[0]["type"] == "N+1"
    assert result.tokens_in == 18
    assert result.tokens_out == 9
