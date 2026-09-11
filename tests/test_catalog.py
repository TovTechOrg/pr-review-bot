"""Mocked-SDK tests for providers/catalog.py -- no live network calls,
ever, per root CLAUDE.md's LLM API testing hygiene section."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from google.auth import exceptions as google_auth_exceptions

from providers import catalog


class _FakeApiError(Exception):
    """Duck-typed stand-in for an SDK error carrying an HTTP-status-shaped
    attribute -- avoids depending on the exact constructor signature of any
    real SDK's exception class."""

    def __init__(self, code: int) -> None:
        super().__init__(f"fake error {code}")
        self.code = code


def _model(name: str, actions=None) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.supported_actions = actions
    return m


class TestListGeminiModels:
    @patch("providers.catalog.genai.Client")
    def test_success_strips_prefix_and_filters_non_generative(self, mock_client_cls):
        client = MagicMock()
        client.models.list.return_value = [
            _model("models/gemini-flash-latest", ["generateContent"]),
            _model("models/embedding-001", ["embedContent"]),
        ]
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.ok is True
        assert result.models == ["gemini-flash-latest"]
        assert result.error is None

    @patch("providers.catalog.genai.Client")
    def test_unauthorized_maps_to_structural_error(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(401)
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("bad-key")

        assert result.ok is False
        assert result.error == "unauthorized"
        assert result.models is None

    @patch("providers.catalog.genai.Client")
    def test_rate_limited_maps_to_structural_error(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(429)
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.error == "rate_limited"

    @patch("providers.catalog.genai.Client")
    def test_unclassified_error_is_provider_unreachable(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = RuntimeError("connection reset")
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.error == "provider_unreachable"


class TestListGroqModels:
    @patch("providers.catalog.Groq")
    def test_success_returns_model_ids(self, mock_groq_cls):
        client = MagicMock()
        response = MagicMock()
        response.data = [MagicMock(id="llama-3.3-70b-versatile"), MagicMock(id="llama3-8b-8192")]
        client.models.list.return_value = response
        mock_groq_cls.return_value = client

        result = catalog.list_groq_models("fake-key")

        assert result.ok is True
        assert result.models == ["llama-3.3-70b-versatile", "llama3-8b-8192"]

    @patch("providers.catalog.Groq")
    def test_forbidden_maps_to_structural_error(self, mock_groq_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(403)
        mock_groq_cls.return_value = client

        result = catalog.list_groq_models("fake-key")

        assert result.ok is False
        assert result.error == "forbidden"


class TestListVertexModels:
    @patch("providers.catalog.genai.Client")
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_success_with_explicit_service_account(self, mock_from_info, mock_client_cls):
        mock_from_info.return_value = MagicMock()
        client = MagicMock()
        client.models.list.return_value = [_model("publishers/google/models/gemini-2.5-flash")]
        mock_client_cls.return_value = client

        result = catalog.list_vertex_models({"project_id": "proj-a", "token_uri": "x"})

        assert result.ok is True
        assert result.models == ["gemini-2.5-flash"]
        mock_client_cls.assert_called_once()
        assert mock_client_cls.call_args.kwargs["project"] == "proj-a"

    def test_no_project_derivable_is_invalid_service_account_json(self):
        result = catalog.list_vertex_models(None)

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("providers.catalog.genai.Client")
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_project_override_takes_precedence(self, mock_from_info, mock_client_cls):
        mock_from_info.return_value = MagicMock()
        client = MagicMock()
        client.models.list.return_value = []
        mock_client_cls.return_value = client

        catalog.list_vertex_models(
            {"project_id": "embedded-proj"}, project_override="candidate-proj"
        )

        assert mock_client_cls.call_args.kwargs["project"] == "candidate-proj"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_bad_service_account_info_is_invalid_service_account_json(self, mock_from_info):
        mock_from_info.side_effect = ValueError("malformed")

        result = catalog.list_vertex_models({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("providers.catalog.genai.Client")
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_refresh_error_is_unauthorized_not_provider_unreachable(
        self, mock_from_info, mock_client_cls
    ):
        from google.auth.exceptions import RefreshError

        mock_from_info.return_value = MagicMock()
        client = MagicMock()
        client.models.list.side_effect = RefreshError("invalid_grant: account disabled")
        mock_client_cls.return_value = client

        result = catalog.list_vertex_models({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "unauthorized"

    @patch("providers.catalog.genai.Client")
    def test_default_credentials_error_is_unauthorized(self, mock_client_cls):
        from google.auth.exceptions import DefaultCredentialsError

        client = MagicMock()
        client.models.list.side_effect = DefaultCredentialsError("no ADC found")
        mock_client_cls.return_value = client

        result = catalog.list_vertex_models(None, project_override="proj-a")

        assert result.ok is False
        assert result.error == "unauthorized"

    @patch("providers.catalog.genai.Client")
    def test_generic_google_auth_error_is_provider_unreachable(self, mock_client_cls):
        from google.auth.exceptions import TransportError

        client = MagicMock()
        client.models.list.side_effect = TransportError("connection reset")
        mock_client_cls.return_value = client

        result = catalog.list_vertex_models(None, project_override="proj-a")

        assert result.ok is False
        assert result.error == "provider_unreachable"


class TestClassifyExceptionPrecedence:
    def test_status_code_wins_over_a_string_code_attribute(self):
        exc = Exception()
        exc.code = "invalid_api_key"  # a Stainless-style string code, not an HTTP status
        exc.status_code = 401

        assert catalog._classify_exception(exc) == "unauthorized"

    def test_int_code_still_used_when_status_code_absent(self):
        exc = Exception()
        exc.code = 429

        assert catalog._classify_exception(exc) == "rate_limited"

    def test_falls_back_to_response_status_code(self):
        exc = Exception()
        exc.response = MagicMock(status_code=403)

        assert catalog._classify_exception(exc) == "forbidden"


class TestVertexCatalogLocations:
    def test_includes_the_default_location(self):
        assert catalog.DEFAULT_VERTEX_LOCATION in catalog.VERTEX_CATALOG_LOCATIONS

    def test_has_no_duplicates(self):
        assert len(catalog.VERTEX_CATALOG_LOCATIONS) == len(set(catalog.VERTEX_CATALOG_LOCATIONS))


class TestListAccessibleProjects:
    def test_no_service_account_info_is_invalid_service_account_json(self):
        result = catalog.list_accessible_projects(None)

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_bad_service_account_info_is_invalid_service_account_json(self, mock_from_info):
        mock_from_info.side_effect = ValueError("malformed")

        result = catalog.list_accessible_projects({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("providers.catalog.AuthorizedSession")
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_success_returns_sorted_deduped_project_ids(self, mock_from_info, mock_session_cls):
        mock_from_info.return_value = MagicMock()
        session = MagicMock()
        session.get.return_value.json.return_value = {
            "projects": [
                {"projectId": "proj-b"},
                {"projectId": "proj-a"},
                {"projectId": "proj-a"},
                {},
            ]
        }
        mock_session_cls.return_value = session

        result = catalog.list_accessible_projects({"project_id": "proj-a"})

        assert result.ok is True
        assert result.models == ["proj-a", "proj-b"]
        session.get.assert_called_once_with(
            catalog._RESOURCE_MANAGER_SEARCH_URL, timeout=catalog._LIST_TIMEOUT_MS / 1000
        )

    @patch("providers.catalog.AuthorizedSession")
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_forbidden_response_maps_to_structural_error(self, mock_from_info, mock_session_cls):
        import requests

        mock_from_info.return_value = MagicMock()
        session = MagicMock()
        error_response = MagicMock(status_code=403)
        session.get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=error_response
        )
        mock_session_cls.return_value = session

        result = catalog.list_accessible_projects({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "forbidden"

    @patch("providers.catalog.AuthorizedSession")
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    def test_refresh_error_is_unauthorized(self, mock_from_info, mock_session_cls):
        from google.auth.exceptions import RefreshError

        mock_from_info.return_value = MagicMock()
        session = MagicMock()
        session.get.side_effect = RefreshError("invalid_grant: account disabled")
        mock_session_cls.return_value = session

        result = catalog.list_accessible_projects({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "unauthorized"


class TestProbeVertexModel:
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_callable_model_returns_ok(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model(
            {"project_id": "proj-a", "token_uri": "x"}, "gemini-2.5-flash"
        )

        assert result.ok is True
        assert result.error is None
        assert result.models is None
        assert client.models.count_tokens.call_args.kwargs["model"] == "gemini-2.5-flash"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_404_is_model_not_callable(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(404)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model(
            {"project_id": "proj-a"}, "gemini-3.1-flash-lite"
        )

        assert result.ok is False
        assert result.error == "model_not_callable"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_rate_limited_is_probe_unavailable_not_model_not_callable(
        self, mock_client_cls, mock_creds
    ):
        """A provider hiccup must never be reported as an unusable model --
        that would send an operator hunting for a replacement model that was
        never the problem."""
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(429)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "model_probe_unavailable"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_server_error_is_probe_unavailable(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(503)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "model_probe_unavailable"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_credential_refresh_failure_is_unauthorized(self, mock_client_cls, mock_creds):
        """A dead credential is a credential problem, not a model problem."""
        client = MagicMock()
        client.models.count_tokens.side_effect = google_auth_exceptions.RefreshError("nope")
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "unauthorized"

    def test_no_project_anywhere_is_invalid_json(self):
        result = catalog.probe_vertex_model({}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_project_and_location_overrides_reach_the_client(
        self, mock_client_cls, mock_creds
    ):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        catalog.probe_vertex_model(
            {"project_id": "key-project"},
            "gemini-2.5-flash",
            project_override="other-project",
            location_override="europe-west4",
        )

        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs["project"] == "other-project"
        assert kwargs["location"] == "europe-west4"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_location_defaults_to_the_module_literal(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert mock_client_cls.call_args.kwargs["location"] == catalog.DEFAULT_VERTEX_LOCATION


class TestProbeGeminiModel:
    @patch("providers.catalog.genai.Client")
    def test_callable_model_returns_ok(self, mock_client_cls):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        result = catalog.probe_gemini_model("fake-key", "gemini-flash-latest")

        assert result.ok is True
        assert result.error is None

    @patch("providers.catalog.genai.Client")
    def test_404_is_model_not_callable(self, mock_client_cls):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(404)
        mock_client_cls.return_value = client

        result = catalog.probe_gemini_model("fake-key", "no-such-model")

        assert result.ok is False
        assert result.error == "model_not_callable"

    @patch("providers.catalog.genai.Client")
    def test_unauthorized_key_is_not_a_model_verdict(self, mock_client_cls):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(401)
        mock_client_cls.return_value = client

        result = catalog.probe_gemini_model("bad-key", "gemini-flash-latest")

        assert result.ok is False
        assert result.error == "unauthorized"
