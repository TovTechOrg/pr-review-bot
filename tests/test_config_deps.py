"""Pure-function tests -- no DB, no mocks, no I/O."""
from __future__ import annotations

from config_deps import (
    conflicts_for,
    credential_slot_vars,
    dependents_of,
    slot_index_for_var,
)


def test_credential_slot_vars_gemini_matches_registry_slots():
    assert credential_slot_vars("gemini") == [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4",
    ]


def test_credential_slot_vars_github_app_is_fixed_pair():
    assert credential_slot_vars("github_app") == ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"]


def test_slot_index_for_var_finds_numbered_slot():
    assert slot_index_for_var("groq", "GROQ_API_KEY_2") == 2


def test_slot_index_for_var_finds_base_slot_as_zero():
    assert slot_index_for_var("vertex", "VERTEX_GCP_SERVICE_ACCOUNT_KEY") == 0


def test_slot_index_for_var_returns_none_for_unrelated_var():
    assert slot_index_for_var("groq", "SOME_OTHER_VAR") is None


def test_slot_index_for_var_returns_none_for_github_app():
    assert slot_index_for_var("github_app", "GITHUB_APP_ID") is None


def test_dependents_of_flags_matching_key_index_override():
    dependents = dependents_of(
        "GEMINI_API_KEY_2",
        key_index_overrides={"gemini": 2},
        provider_override="groq",
    )
    assert dependents is not None
    assert dependents.key_index_override is True
    assert dependents.provider_override is False
    assert dependents.labels() == ["key_index override"]


def test_dependents_of_flags_active_provider():
    dependents = dependents_of(
        "GEMINI_API_KEY", key_index_overrides={}, provider_override="gemini"
    )
    assert dependents.provider_override is True
    assert dependents.any() is True


def test_dependents_of_returns_none_when_nothing_points_at_it():
    dependents = dependents_of(
        "GROQ_API_KEY_3", key_index_overrides={"groq": 1}, provider_override="gemini"
    )
    assert dependents is not None
    assert dependents.any() is False


def test_dependents_of_returns_none_for_non_credential_var():
    assert dependents_of("VERTEX_GCP_PROJECT", key_index_overrides={}, provider_override=None) is None


def test_dependents_of_does_not_flag_provider_for_an_inactive_spare_slot():
    """Regression test: deleting a spare, unused slot for the active
    provider must not report the provider override as a dependent -- only
    deleting the slot actually in use should."""
    dependents = dependents_of(
        "GEMINI_API_KEY_3", key_index_overrides={"gemini": 0}, provider_override="gemini"
    )
    assert dependents.key_index_override is False
    assert dependents.provider_override is False
    assert dependents.any() is False


def test_dependents_of_flags_provider_when_deleting_the_actually_active_slot():
    dependents = dependents_of(
        "GEMINI_API_KEY_2", key_index_overrides={"gemini": 2}, provider_override="gemini"
    )
    assert dependents.key_index_override is True
    assert dependents.provider_override is True


def test_conflicts_for_flags_project_mismatch():
    conflicts = conflicts_for("vertex", "new-project", "old-project")
    assert conflicts == [{"var": "VERTEX_GCP_PROJECT", "current": "old-project", "new": "new-project"}]


def test_conflicts_for_no_conflict_when_projects_match():
    assert conflicts_for("vertex", "same", "same") == []


def test_conflicts_for_empty_when_no_current_value_set():
    assert conflicts_for("vertex", "new-project", None) == []


def test_conflicts_for_empty_for_non_vertex_family():
    assert conflicts_for("gemini", "x", "y") == []
