"""Tests for specialists/security.py."""

from __future__ import annotations

from specialists.security import (
    SECURITY_SYSTEM_PROMPT,
    SecurityFindings,
    run_security_specialist,
)
from specialists.schemas import SecurityFinding


def test_security_findings_container_wraps_list_of_security_finding():
    container = SecurityFindings(
        findings=[
            SecurityFinding(
                severity="critical",
                file="app.py",
                line=14,
                description="Hardcoded API key",
                fix="Move to env var",
            )
        ]
    )
    assert len(container.findings) == 1
    assert container.findings[0].severity == "critical"


def test_security_system_prompt_mentions_key_risk_categories():
    prompt_lower = SECURITY_SYSTEM_PROMPT.lower()
    for keyword in ("credential", "injection", "deserializ"):
        assert keyword in prompt_lower


async def test_run_security_specialist_success(monkeypatch):
    from providers.base import LLMResponse

    parsed = SecurityFindings(
        findings=[
            SecurityFinding(
                severity="critical",
                file="app.py",
                line=14,
                description="Hardcoded API key",
                fix="Move to env var",
            )
        ]
    )

    class FakeProvider:
        async def complete(self, system, user, schema, **kwargs):
            assert schema is SecurityFindings
            return LLMResponse(raw_text="{}", tokens_in=20, tokens_out=10, parsed=parsed)

    monkeypatch.setattr("specialists.base.get_provider", lambda: FakeProvider())

    result = await run_security_specialist(
        "annotated diff text",
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.name == "Security"
    assert result.status == "ok"
    assert result.findings[0]["description"] == "Hardcoded API key"
    assert result.tokens_in == 20
    assert result.tokens_out == 10
