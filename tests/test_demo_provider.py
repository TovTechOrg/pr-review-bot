import pytest

from demo.content import DEMO_DIFF, DEMO_PR_NUMBER, DEMO_REPO
from demo.provider import MockProvider, demo_provider_and_model
from diff_utils import annotate_and_cap
from providers.pricing import estimate_cost_usd
from specialists.quality import QualityFindings
from specialists.performance import PerformanceFindings
from specialists.security import SecurityFindings


def test_diff_is_a_real_unified_diff_with_added_lines():
    # annotate_and_cap yields nothing for a diff with no added lines, and an
    # empty annotated diff makes the orchestrator return ReviewSkipped.
    assert DEMO_DIFF.startswith("diff --git ")
    assert "@@" in DEMO_DIFF
    assert any(line.startswith("+") and not line.startswith("+++")
               for line in DEMO_DIFF.splitlines())


def test_identity_is_the_agreed_fake():
    assert DEMO_REPO == "bot-demo/example-app"
    assert DEMO_PR_NUMBER == 7


@pytest.mark.parametrize("schema", [SecurityFindings, PerformanceFindings, QualityFindings])
async def test_provider_returns_one_parsed_finding_per_specialist(schema):
    provider = MockProvider(provider="groq", model="llama-3.3-70b-versatile")
    response = await provider.complete(
        "system", "user", schema,
        timeout_seconds=30.0, default_retry_after_seconds=1.0,
    )
    assert isinstance(response.parsed, schema)
    assert len(response.parsed.findings) == 1
    assert response.tokens_in > 0 and response.tokens_out > 0


@pytest.mark.parametrize("requested", [None, "gemini", "vertex", "groq", "nonsense"])
def test_every_resolved_pair_is_priced(requested):
    provider, model = demo_provider_and_model(requested)
    assert estimate_cost_usd(provider, model, 1000, 100) is not None


def test_a_requested_model_in_the_providers_own_catalog_is_honored():
    """The wizard's LLM frame lets a reader pick a SPECIFIC model, not just a
    provider -- demo_provider_and_model must report that exact pick back, or
    the mocked review's provider/model line lies about what was chosen."""
    from demo.model_catalog import MODELS_BY_PROVIDER

    for provider, models in MODELS_BY_PROVIDER.items():
        for model in models:
            assert demo_provider_and_model(provider, model) == (provider, model)


def test_a_model_belonging_to_a_different_provider_falls_back_to_the_default():
    """A gemini model name requested for vertex (or any other mismatch) must
    not be reported/priced as if vertex had actually run it."""
    from demo.model_catalog import MODELS_BY_PROVIDER

    gemini_only_model = MODELS_BY_PROVIDER["gemini"][0]
    provider, model = demo_provider_and_model("vertex", gemini_only_model)
    assert provider == "vertex"
    assert model == MODELS_BY_PROVIDER["vertex"][0]


def test_gemini_catalog_matches_the_onboarding_wizards_offered_models():
    """Regression for a 2026-09-18 end-to-end finding: picking gemini +
    gemini-2.5-pro in the wizard's LLM frame silently reported
    gemini-flash-latest in the demo dashboard's mocked review, because this
    repo's demo/model_catalog.py hadn't caught up with onboarding-wizard's
    own demo/content.py::GEMINI_MODELS (which already offered it). The two
    lists can't share Python across the repo boundary, so this pins the
    values by hand -- see demo/model_catalog.py's own docstring."""
    from demo.model_catalog import MODELS_BY_PROVIDER

    assert MODELS_BY_PROVIDER["gemini"] == [
        "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro",
    ]
    assert MODELS_BY_PROVIDER["groq"] == [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    ]
    assert MODELS_BY_PROVIDER["vertex"] == ["gemini-flash-latest", "gemini-2.5-flash"]


def test_an_unrecognized_model_falls_back_to_the_priced_default():
    from demo.model_catalog import MODELS_BY_PROVIDER

    provider, model = demo_provider_and_model("groq", "not-a-real-model")
    assert provider == "groq"
    assert model == MODELS_BY_PROVIDER["groq"][0]
    assert estimate_cost_usd(provider, model, 1000, 100) is not None


def test_demo_diff_headers_parse_correctly_and_all_files_appear():
    # Regression test for headers with stray leading spaces or typos that prevent
    # diff_utils.annotate_and_cap from correctly detecting file boundaries and
    # attributing added lines to the right files. Stray spaces in the diff --git
    # line would cause that file's hunks to be skipped entirely. All three files
    # must appear in the annotated output.
    annotated = annotate_and_cap(DEMO_DIFF)
    annotated_text = annotated.text

    # Verify all three files appear in the annotated diff
    assert "diff --git a/app/auth.py b/app/auth.py" in annotated_text
    assert "diff --git a/app/api/users.py b/app/api/users.py" in annotated_text
    assert "diff --git a/app/utils/format.py b/app/utils/format.py" in annotated_text

    # Verify expected added lines are attributed to the correct files
    assert "app/auth.py:87:" in annotated_text  # First added line in auth.py
    assert "app/api/users.py:144:" in annotated_text  # First added line in users.py
