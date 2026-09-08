"""Deterministic tests for the provider layer (providers/*).

Mocks the actual `google.genai.Client` boundary (`client.aio.models.generate_content`)
so nothing here ever makes a real network call. Live verification against the
real Gemini API happens separately in scripts/manual_verify_step4.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from config import settings
from providers import pricing
from providers.factory import get_provider
from providers.google_genai import GeminiProvider, VertexProvider
from providers.validate import validate_and_repair

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    from providers.factory import reset_provider_cache

    reset_provider_cache()
    yield
    reset_provider_cache()


@pytest.fixture(autouse=True)
def _reset_active_overrides():
    """Several tests below set overrides in providers.active,
    active_model, and factory to exercise the model-is-part-of-the-cache-key
    behavior. Resetting only at the end of the test body means an earlier
    assertion failure skips cleanup and pollutes every later test in the same
    run -- reset both before and after, mirroring
    tests/test_active_model.py's autouse fixture."""
    from providers import active, active_model, factory

    active.reset_override_cache()
    active_model.reset_override_cache()
    factory.reset_provider_cache()
    yield
    active.reset_override_cache()
    active_model.reset_override_cache()
    factory.reset_provider_cache()


def test_importing_the_factory_alone_does_not_pull_in_provider_sdks():
    """providers/factory.py used to import GeminiProvider/VertexProvider
    and GroqProvider unconditionally at module level, so every test or
    request path that transitively imports the factory (specialists/base.py
    is the sole importer) paid the full google.genai SDK import cost --
    measured via `-X importtime` at 4.2s across 323 modules, the single
    largest import in a full collection pass, bigger than pytest itself --
    even when it never touches Gemini/Vertex/Groq. _build() already branches
    on `provider` before constructing, so the fix moves both imports inside
    the branch that actually needs them.

    Checked in a fresh subprocess: this test process has already imported
    both SDKs via other test files (e.g. this file's own module-level
    `from providers.google_genai import ...` above), so sys.modules
    here would give a false pass.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import providers.factory\n"
            "print('google.genai' in sys.modules)\n"
            "print('groq' in sys.modules)\n",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    genai_imported, groq_imported = result.stdout.strip().splitlines()
    assert genai_imported == "False", "importing factory alone must not import google.genai"
    assert groq_imported == "False", "importing factory alone must not import groq"


class Greeting(BaseModel):
    message: str


def _fake_response(text: str, tokens_in: int = 10, tokens_out: int = 5, thoughts_tokens: int = 0):
    """Build a stand-in for google.genai.types.GenerateContentResponse."""
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=tokens_in,
            candidates_token_count=tokens_out,
            thoughts_token_count=thoughts_tokens,
        ),
    )


# --------------------------------------------------------------------------
# google_genai.py — both client shapes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_provider_parses_valid_structured_output(monkeypatch):
    fake_generate = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 42, 7))
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(
        api_key="dummy-key-for-construction-only", model=settings.gemini_model
    )
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    fake_generate.assert_awaited_once()
    _, kwargs = fake_generate.call_args
    assert kwargs["model"] == settings.gemini_model


@pytest.mark.asyncio
async def test_gemini_provider_includes_thinking_tokens_in_tokens_out(monkeypatch):
    """Google bills thinking tokens (thoughts_token_count) at the output
    rate, but they're excluded from candidates_token_count -- tokens_out
    must add them in, or cost estimates and the usage cap both silently
    under-count for any thinking-enabled model (the default for both
    gemini-flash-latest and gemini-2.5-flash)."""
    fake_generate = AsyncMock(
        return_value=_fake_response(
            json.dumps({"message": "hi"}), tokens_in=42, tokens_out=7, thoughts_tokens=15
        )
    )
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(
        api_key="dummy-key-for-construction-only", model=settings.gemini_model
    )
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.tokens_out == 22  # 7 candidates + 15 thinking


@pytest.mark.asyncio
async def test_provider_returns_none_parsed_on_malformed_json(monkeypatch):
    fake_generate = AsyncMock(return_value=_fake_response("not json at all", 10, 1))
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(
        api_key="dummy-key-for-construction-only", model=settings.gemini_model
    )
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None
    assert result.tokens_in == 10
    assert result.tokens_out == 1


@pytest.mark.asyncio
async def test_provider_returns_none_parsed_on_off_schema_json(monkeypatch):
    fake_generate = AsyncMock(
        return_value=_fake_response(json.dumps({"totally": "wrong shape"}), 10, 1)
    )
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(
        api_key="dummy-key-for-construction-only", model=settings.gemini_model
    )
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None


def _fake_client_factory(captured: dict, fake_generate):
    def _build(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        )

    return _build


@pytest.mark.asyncio
async def test_vertex_provider_parses_valid_structured_output(monkeypatch):
    """Same call, same parsing, same usage accounting as gemini -- the only
    difference between the two adapters is how the client is authenticated."""
    captured: dict = {}
    fake_generate = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 42, 7))
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        _fake_client_factory(captured, fake_generate),
    )

    provider = VertexProvider(
        project="proj-x", location="us-central1", service_account_info=None,
        model=settings.gemini_model,
    )
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    assert captured["vertexai"] is True
    assert captured["project"] == "proj-x"
    assert captured["location"] == "us-central1"
    _, kwargs = fake_generate.call_args
    assert kwargs["model"] == settings.gemini_model


def test_vertex_provider_passes_no_credentials_for_implicit_adc(monkeypatch):
    """credentials=None is genai.Client's own default, and passing it
    explicitly is identical to omitting it -- which is exactly what makes
    google-auth discover the local ADC file."""
    captured: dict = {}
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        _fake_client_factory(captured, AsyncMock()),
    )

    VertexProvider(
        project="proj-x", location="us-central1", service_account_info=None,
        model=settings.gemini_model,
    )

    assert captured["credentials"] is None


def test_vertex_provider_builds_credentials_from_the_service_account_info(monkeypatch):
    """from_service_account_info is mocked: a real one needs a real RSA private
    key, and this test is about the wiring, not about google-auth's parsing.
    Also pins the OAuth scope: without it, the resulting credentials produce
    an empty-scope JWT assertion that Google's token endpoint rejects with
    invalid_scope -- the exact failure a real live call against this code hit."""
    captured: dict = {}
    sentinel = object()
    seen: dict = {}
    monkeypatch.setattr(
        "providers.google_genai.genai.Client",
        _fake_client_factory(captured, AsyncMock()),
    )

    def _fake_from_service_account_info(info, scopes=None):
        seen.update(info)
        seen["_scopes"] = scopes
        return sentinel

    monkeypatch.setattr(
        "providers.google_genai.service_account.Credentials.from_service_account_info",
        _fake_from_service_account_info,
    )

    info = {"type": "service_account", "project_id": "proj-x"}
    VertexProvider(
        project="proj-x", location="us-central1", service_account_info=info,
        model=settings.gemini_model,
    )

    assert captured["credentials"] is sentinel
    assert seen["type"] == "service_account"
    assert seen["project_id"] == "proj-x"
    assert seen["_scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]


# --------------------------------------------------------------------------
# factory.py
# --------------------------------------------------------------------------


def test_factory_selects_gemini(monkeypatch):
    # google-genai's Client raises immediately on an empty api_key, so a
    # fresh checkout with no real .env (e.g. CI) needs a dummy non-empty
    # value here — this test must not depend on real credentials existing.
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "dummy-key-for-construction-only")
    assert isinstance(get_provider(), GeminiProvider)


def test_factory_selects_groq(monkeypatch):
    # _build()'s new empty-credential check (added by this task) now raises
    # before construction, so this test needs a non-empty value — previously an
    # empty string reached GroqProvider.__init__ successfully, since Groq's SDK
    # only rejects api_key=None, not an empty string.
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "dummy-key-for-construction-only")
    from providers.groq import GroqProvider

    assert isinstance(get_provider(), GroqProvider)


def test_factory_raises_for_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "bogus")
    with pytest.raises(ValueError):
        get_provider()


def test_factory_raises_a_clear_error_for_an_unprovisioned_key_index(monkeypatch):
    """The locally-detectable invalid-state case: activating gemini at index 1
    when only GEMINI_API_KEY (index 0) exists anywhere. Distinct from a
    dead-but-configured provider (a real credential for a vendor that's down
    or retired), which must NOT be affected by this check -- that case has a
    real, non-empty credential and fails at the live call, unchanged."""
    from providers import key_index
    from providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_index_0")
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    reset_provider_cache()
    key_index.set_override_cache({"gemini": 1})

    with pytest.raises(ValueError) as exc:
        get_provider()
    assert "GEMINI_API_KEY_1" in str(exc.value)
    assert "gemini" in str(exc.value)
    assert "1" in str(exc.value)

    key_index.reset_override_cache()
    reset_provider_cache()


def test_factory_unaffected_by_a_dead_but_configured_provider(monkeypatch):
    """A real, non-empty credential must still reach client construction --
    this check only catches an EMPTY resolved value, nothing else."""
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_real_but_dead")
    from providers.groq import GroqProvider

    assert isinstance(get_provider(), GroqProvider)


def test_factory_returns_the_same_instance_on_repeated_calls(monkeypatch):
    from providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    # _build()'s empty-credential check requires a non-empty api_key.
    monkeypatch.setattr(settings, "groq_api_key", "dummy-key-for-construction-only")
    reset_provider_cache()
    first = get_provider()
    second = get_provider()
    assert first is second
    reset_provider_cache()


def test_factory_rebuilds_the_client_when_the_key_index_changes(monkeypatch):
    from providers import key_index
    from providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_index_0")
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_index_1")
    reset_provider_cache()
    key_index.reset_override_cache()

    at_index_0 = get_provider()
    key_index.set_override_cache({"groq": 1})
    at_index_1 = get_provider()

    assert at_index_0 is not at_index_1
    key_index.reset_override_cache()
    reset_provider_cache()


def test_factory_returns_to_the_original_cached_instance_after_switching_back(monkeypatch):
    from providers import key_index
    from providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_index_0")
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_index_1")
    reset_provider_cache()
    key_index.reset_override_cache()

    at_index_0 = get_provider()
    key_index.set_override_cache({"groq": 1})
    get_provider()
    key_index.reset_override_cache()
    back_at_index_0 = get_provider()

    assert back_at_index_0 is at_index_0
    key_index.reset_override_cache()
    reset_provider_cache()


def _mock_vertex_client(monkeypatch, captured: dict | None = None):
    """genai.Client would otherwise try to authenticate for real at
    construction; these tests are about _build's own branching. Pass a dict to
    capture the kwargs it was constructed with."""

    def _build(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace()))

    monkeypatch.setattr("providers.google_genai.genai.Client", _build)


def test_factory_selects_vertex_and_derives_the_project_from_the_key(monkeypatch):
    """VERTEX_GCP_PROJECT unset is the COMMON case: an operator handed nothing but a
    service-account JSON key gets the project from the key's own project_id."""
    from providers.google_genai import VertexProvider

    captured: dict = {}
    _mock_vertex_client(monkeypatch, captured)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_gcp_project", "")
    monkeypatch.setattr(settings, "vertex_gcp_location", "us-central1")
    monkeypatch.setattr(
        "providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: {"type": "service_account", "project_id": "proj-from-key"},
    )
    monkeypatch.setattr(
        "providers.google_genai.service_account.Credentials.from_service_account_info",
        lambda info, scopes=None: object(),
    )

    assert isinstance(get_provider(), VertexProvider)
    assert captured["project"] == "proj-from-key"
    assert captured["location"] == "us-central1"


def test_factory_prefers_an_explicit_gcp_project_over_the_keys_own(monkeypatch):
    """VERTEX_GCP_PROJECT still exists as an override -- for pointing a key at a
    different project than the one it was minted in."""
    captured: dict = {}
    _mock_vertex_client(monkeypatch, captured)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_gcp_project", "proj-explicit")
    monkeypatch.setattr(
        "providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: {"type": "service_account", "project_id": "proj-from-key"},
    )
    monkeypatch.setattr(
        "providers.google_genai.service_account.Credentials.from_service_account_info",
        lambda info, scopes=None: object(),
    )

    get_provider()
    assert captured["project"] == "proj-explicit"


def test_factory_builds_vertex_from_implicit_adc_when_a_project_is_set(monkeypatch):
    """The one behavioral difference from gemini/groq worth its own test: an
    EMPTY resolved credential is not an error for vertex. _build must not
    raise -- any failure then comes from the SDK/google-auth relying on
    implicit ADC, which is a live-call concern, not a config one."""
    from providers.google_genai import VertexProvider

    _mock_vertex_client(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_gcp_project", "proj-explicit")
    monkeypatch.setattr(
        "providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: None,
    )

    assert isinstance(get_provider(), VertexProvider)


def test_factory_raises_when_vertex_has_neither_a_project_nor_a_credential(monkeypatch):
    """Pure implicit-ADC with no key to derive a project from: locally
    detectable, so it must fast-fail before any network call rather than let
    three specialists each discover the same problem the expensive way."""
    _mock_vertex_client(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_gcp_project", "")
    monkeypatch.setattr(
        "providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: None,
    )

    with pytest.raises(ValueError) as exc:
        get_provider()
    assert "vertex" in str(exc.value)
    assert "VERTEX_GCP_PROJECT" in str(exc.value)


def test_factory_passes_the_active_key_index_to_vertex_credentials(monkeypatch):
    """vertex rides the same key-index override as gemini/groq -- the index
    must reach the credential resolver, or a slot swap would be a silent
    no-op for this provider alone."""
    from providers import key_index
    from providers.factory import reset_provider_cache

    _mock_vertex_client(monkeypatch)
    seen: list[int] = []
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_gcp_project", "proj-explicit")
    monkeypatch.setattr(
        "providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: seen.append(index) or None,
    )
    reset_provider_cache()
    key_index.set_override_cache({"vertex": 2})

    get_provider()
    assert seen == [2]

    key_index.reset_override_cache()
    reset_provider_cache()


# --------------------------------------------------------------------------
# validate.py — validate-and-repair
# --------------------------------------------------------------------------


class FakeProvider:
    """A scripted LLMProvider: returns each entry in `responses`, in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, schema):
        self.calls.append((system, user))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_validate_and_repair_succeeds_on_first_try():
    from providers.base import LLMResponse

    provider = FakeProvider(
        [
            LLMResponse(
                raw_text='{"message": "hi"}',
                tokens_in=5,
                tokens_out=2,
                parsed=Greeting(message="hi"),
            )
        ]
    )

    result = await validate_and_repair(provider, "sys", "usr", Greeting)

    assert result.ok is True
    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 5
    assert result.tokens_out == 2
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_validate_and_repair_retries_once_then_succeeds():
    from providers.base import LLMResponse

    provider = FakeProvider(
        [
            LLMResponse(raw_text="garbage", tokens_in=10, tokens_out=1, parsed=None),
            LLMResponse(
                raw_text='{"message": "fixed"}',
                tokens_in=8,
                tokens_out=3,
                parsed=Greeting(message="fixed"),
            ),
        ]
    )

    result = await validate_and_repair(provider, "sys", "usr", Greeting)

    assert result.ok is True
    assert result.parsed == Greeting(message="fixed")
    assert result.tokens_in == 18  # accumulated across both attempts
    assert result.tokens_out == 4
    assert len(provider.calls) == 2
    # repair prompt should differ from the original system prompt
    assert provider.calls[1][0] != provider.calls[0][0]
    assert "sys" in provider.calls[1][0]


@pytest.mark.asyncio
async def test_validate_and_repair_fails_after_repair_also_fails():
    from providers.base import LLMResponse

    provider = FakeProvider(
        [
            LLMResponse(raw_text="garbage", tokens_in=10, tokens_out=1, parsed=None),
            LLMResponse(raw_text="still garbage", tokens_in=8, tokens_out=1, parsed=None),
        ]
    )

    result = await validate_and_repair(provider, "sys", "usr", Greeting)

    assert result.ok is False
    assert result.parsed is None
    assert result.error is not None
    assert result.tokens_in == 18
    assert result.tokens_out == 2
    assert len(provider.calls) == 2


# --------------------------------------------------------------------------
# pricing.py
# --------------------------------------------------------------------------


def test_estimate_cost_usd_gemini_flash():
    cost = pricing.estimate_cost_usd(
        "gemini", "gemini-flash-latest", tokens_in=4_000, tokens_out=500
    )
    # 4000/1e6 * 0.30 + 500/1e6 * 2.50
    assert cost == pytest.approx(0.0012 + 0.00125)


def test_estimate_cost_usd_unknown_model_returns_none():
    assert pricing.estimate_cost_usd("gemini", "no-such-model", tokens_in=1, tokens_out=1) is None


def test_estimate_cost_usd_groq_llama():
    cost = pricing.estimate_cost_usd(
        "groq", "llama-3.3-70b-versatile", tokens_in=4_000, tokens_out=500
    )
    # 4000/1e6 * 0.59 + 500/1e6 * 0.79
    assert cost == pytest.approx(0.00236 + 0.000395)


def test_estimate_cost_usd_groq_unknown_model_returns_none():
    assert pricing.estimate_cost_usd("groq", "no-such-model", tokens_in=1, tokens_out=1) is None


def test_estimate_cost_usd_vertex_flash():
    """Same model, same published rate as AI-Studio's paid tier -- the two
    providers differ in the auth path, not in what a token costs."""
    cost = pricing.estimate_cost_usd(
        "vertex", "gemini-flash-latest", tokens_in=4_000, tokens_out=500
    )
    assert cost == pytest.approx(0.0012 + 0.00125)


def test_estimate_cost_usd_vertex_gemini_2_5_flash():
    """The model actually confirmed live for vertex (see ISSUES.md) --
    gemini-flash-latest doesn't exist as a Vertex publisher model for every
    project/region, so a real deployment needs this entry to avoid a
    KeyError after a successful live call."""
    cost = pricing.estimate_cost_usd(
        "vertex", "gemini-2.5-flash", tokens_in=4_000, tokens_out=500
    )
    assert cost == pytest.approx(0.0012 + 0.00125)


# --------------------------------------------------------------------------
# active_model.py <-> factory.py -- model is part of the provider cache key
# --------------------------------------------------------------------------


def test_a_model_change_is_a_cache_miss(monkeypatch):
    """Adapters bake the model in at construction and factory._instances is
    process-lifetime, so a model override would silently no-op on a warm
    process unless the model is part of the cache key."""
    from config import settings
    from providers import active, active_model, factory, key_index

    factory.reset_provider_cache()
    active.set_override_cache("groq")
    key_index.reset_override_cache()
    monkeypatch.setattr(settings, "groq_api_key", "sentinel-key")
    monkeypatch.setattr(settings, "groq_model", "model-a")

    first = factory.get_provider()
    active_model.set_override_cache({"groq": "model-b"})
    second = factory.get_provider()

    assert first is not second
    assert first._model == "model-a"
    assert second._model == "model-b"


def test_gemini_provider_uses_the_db_override_not_settings_gemini_model(monkeypatch):
    """Tautology guard for GeminiProvider: test_gemini_provider_parses_valid_
    structured_output above asserts kwargs["model"] == settings.gemini_model on
    BOTH sides of the comparison, so it would not catch a regression where
    GeminiProvider.__init__ silently went back to reading settings.gemini_model
    internally instead of using its constructor argument. Setting a DB model
    override to a sentinel that DIFFERS from settings.gemini_model, and asserting
    the constructed instance's _model equals the sentinel (not settings.gemini_model),
    proves the constructor argument is what actually populates self._model --
    the single most important correctness property this branch adds (the model
    reported in the PR comment must equal the model actually sent)."""
    from providers import active_model, factory

    factory.reset_provider_cache()
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "dummy-key-for-construction-only")
    monkeypatch.setattr(settings, "gemini_model", "settings-model-must-not-be-used")
    active_model.set_override_cache({"gemini": "sentinel-gemini-model"})

    provider = factory.get_provider()

    assert isinstance(provider, GeminiProvider)
    assert provider._model == "sentinel-gemini-model"
    assert provider._model != settings.gemini_model


def test_vertex_provider_uses_the_db_override_not_settings_vertex_model(monkeypatch):
    """Same tautology guard as the gemini test above, for VertexProvider: a
    regression to reading settings.vertex_model internally would go uncaught
    by the existing vertex tests, which all pass settings.gemini_model/whatever
    the ambient value is on both sides of their assertions."""
    from providers import active_model, factory

    factory.reset_provider_cache()
    _mock_vertex_client(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_gcp_project", "proj-explicit")
    monkeypatch.setattr(settings, "vertex_model", "settings-model-must-not-be-used")
    monkeypatch.setattr(
        "providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: None,
    )
    active_model.set_override_cache({"vertex": "sentinel-vertex-model"})

    provider = factory.get_provider()

    assert isinstance(provider, VertexProvider)
    assert provider._model == "sentinel-vertex-model"
    assert provider._model != settings.vertex_model


def test_reported_model_equals_executed_model(monkeypatch):
    """orchestrator._active_model() feeds the PR comment; the adapter's
    _model is what actually runs. If these diverge, the comment reports a
    model that never ran -- a silent partial failure."""
    import orchestrator
    from config import settings
    from providers import active, active_model, factory, key_index

    factory.reset_provider_cache()
    active.set_override_cache("groq")
    key_index.reset_override_cache()
    monkeypatch.setattr(settings, "groq_api_key", "sentinel-key")
    monkeypatch.setattr(settings, "groq_model", "model-a")
    active_model.set_override_cache({"groq": "model-b"})

    assert orchestrator._active_model() == factory.get_provider()._model
