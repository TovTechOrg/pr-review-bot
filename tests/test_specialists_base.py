"""Tests for specialists/base.py — the shared Specialist.run() shape."""

from __future__ import annotations

from pydantic import BaseModel

from specialists.base import run_specialist


class DummyFinding(BaseModel):
    note: str


class DummyFindings(BaseModel):
    findings: list[DummyFinding]


class FakeProvider:
    def __init__(self, response):
        self._response = response

    async def complete(self, system, user, schema, **kwargs):
        return self._response


async def test_run_specialist_success_populates_findings_and_usage(monkeypatch):
    from providers.base import LLMResponse

    parsed = DummyFindings(findings=[DummyFinding(note="a"), DummyFinding(note="b")])
    fake = FakeProvider(LLMResponse(raw_text="{}", tokens_in=10, tokens_out=5, parsed=parsed))
    monkeypatch.setattr("specialists.base.get_provider", lambda: fake)

    result = await run_specialist(
        name="Security",
        annotated_diff="some diff",
        system_prompt="do a review",
        container_schema=DummyFindings,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.name == "Security"
    assert result.status == "ok"
    assert result.findings == [{"note": "a"}, {"note": "b"}]
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.error is None
    assert result.elapsed_ms >= 0


async def test_run_specialist_failure_never_raises(monkeypatch):
    from providers.base import LLMResponse

    fake = FakeProvider(LLMResponse(raw_text="garbage", tokens_in=1, tokens_out=1, parsed=None))
    monkeypatch.setattr("specialists.base.get_provider", lambda: fake)

    result = await run_specialist(
        name="Security",
        annotated_diff="some diff",
        system_prompt="do a review",
        container_schema=DummyFindings,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.status == "failed"
    assert result.findings == []
    assert result.error is not None


async def test_run_specialist_never_raises_on_provider_exception(monkeypatch):
    class ExplodingProvider:
        async def complete(self, system, user, schema, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("specialists.base.get_provider", lambda: ExplodingProvider())

    result = await run_specialist(
        name="Security",
        annotated_diff="some diff",
        system_prompt="do a review",
        container_schema=DummyFindings,
        timeout_seconds=45.0,
        default_retry_after_seconds=60.0,
    )

    assert result.status == "failed"
    assert "boom" in result.error
