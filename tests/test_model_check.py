"""Mocked tests for providers/model_check.py -- the single predicate every
slot_config writer gates on. No live calls (root CLAUDE.md)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from providers import catalog, model_check


def _ok():
    return catalog.CatalogResult(ok=True, models=None, error=None)


def _fail(error: str):
    return catalog.CatalogResult(ok=False, models=None, error=error)


class TestGroqIsNeverProbed:
    def test_groq_returns_no_problems_without_calling_any_probe(self):
        with patch.object(catalog, "probe_gemini_model") as probe:
            assert model_check.problems("groq", 0, "llama-3.3-70b-versatile") == []
            probe.assert_not_called()


class TestVertex:
    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_callable_model_has_no_problems(self, resolve, probe):
        resolve.return_value = {"project_id": "proj-a"}
        probe.return_value = _ok()

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == []

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_uncallable_model_reports_the_probe_code(self, resolve, probe):
        resolve.return_value = {"project_id": "proj-a"}
        probe.return_value = _fail("model_not_callable")

        assert model_check.problems("vertex", 0, "gemini-3.1-flash-lite") == [
            "model_not_callable"
        ]

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_probe_unavailable_is_a_problem_too(self, resolve, probe):
        """Fail closed: an unreachable provider must not let an unverified
        model through, or 'saved unverified' silently becomes 'saved broken'
        -- nothing re-checks it later."""
        resolve.return_value = {"project_id": "proj-a"}
        probe.return_value = _fail("model_probe_unavailable")

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == [
            "model_probe_unavailable"
        ]

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_slot_project_and_location_reach_the_probe(self, resolve, probe):
        resolve.return_value = {"project_id": "key-project"}
        probe.return_value = _ok()

        model_check.problems(
            "vertex",
            2,
            "gemini-2.5-flash",
            vertex_gcp_project="other-project",
            vertex_gcp_location="europe-west4",
        )

        assert probe.call_args.kwargs["project_override"] == "other-project"
        assert probe.call_args.kwargs["location_override"] == "europe-west4"
        assert resolve.call_args.args[0] == 2

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_malformed_stored_credential_is_structural_not_a_crash(self, resolve, probe):
        resolve.side_effect = ValueError("not base64")

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == [
            "invalid_service_account_json"
        ]
        probe.assert_not_called()

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_no_key_and_no_project_is_no_credential_configured(self, resolve, probe):
        """Mirrors dashboard _safe_resolve_vertex_info: a missing key is only
        a problem when the slot has no project either, since without either
        there is nothing for implicit ADC to resolve against."""
        resolve.return_value = None

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == [
            "no_credential_configured"
        ]
        probe.assert_not_called()

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_no_key_but_a_project_falls_through_to_implicit_adc(self, resolve, probe):
        resolve.return_value = None
        probe.return_value = _ok()

        assert model_check.problems(
            "vertex", 0, "gemini-2.5-flash", vertex_gcp_project="proj-a"
        ) == []
        assert probe.call_args.args[0] is None


class TestExplicitCredential:
    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_passed_credential_is_probed_and_the_slot_is_never_resolved(
        self, resolve, probe
    ):
        """Guided setup probes a credential that exists nowhere the slot
        could resolve it from yet. Resolving the slot here would probe the
        PREVIOUS credential -- or none at all on a first-time setup -- and
        answer a question nobody asked. resolve raising is the assertion."""
        resolve.side_effect = AssertionError("must not resolve the slot")
        probe.return_value = _ok()

        assert model_check.problems(
            "vertex", 0, "gemini-2.5-flash", credential={"project_id": "fresh"}
        ) == []
        assert probe.call_args.args[0] == {"project_id": "fresh"}

    @patch.object(catalog, "probe_gemini_model")
    @patch("providers.model_check.credentials.resolve")
    def test_passed_api_key_is_probed_for_gemini(self, resolve, probe):
        resolve.side_effect = AssertionError("must not resolve the slot")
        probe.return_value = _ok()

        assert model_check.problems(
            "gemini", 0, "gemini-flash-latest", credential="fresh-key"
        ) == []
        assert probe.call_args.args[0] == "fresh-key"


class TestGemini:
    @patch.object(catalog, "probe_gemini_model")
    @patch("providers.model_check.credentials.resolve")
    def test_missing_key_is_no_credential_configured(self, resolve, probe):
        resolve.return_value = ("GEMINI_API_KEY", "")

        assert model_check.problems("gemini", 0, "gemini-flash-latest") == [
            "no_credential_configured"
        ]
        probe.assert_not_called()


class TestStructuralGuards:
    def test_unknown_provider(self):
        assert model_check.problems("openai", 0, "gpt-5") == ["unknown_provider"]

    @pytest.mark.parametrize("model", ["", None])
    def test_empty_model_is_a_problem_before_any_probe(self, model):
        with patch.object(catalog, "probe_vertex_model") as probe:
            assert model_check.problems("vertex", 0, model) == ["model_required"]
            probe.assert_not_called()
