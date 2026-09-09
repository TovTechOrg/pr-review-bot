"""The DB-backed provider override: a singleton row that lets the hosted
service swap providers without a redeploy.

Uses the shared Postgres test harness (``db`` from tests/conftest.py).

Note on ordering: ``test_override_defaults_to_none`` is declared *last*, not
first. This repo runs plain ``pytest`` with no ordering/randomization plugin,
so collection is strictly top-to-bottom -- if that test were declared first
it would always run before any test that sets an override, and could never
catch a regression where the ``db`` fixture stops truncating
``runtime_config`` between tests. Declaring it last means several preceding
tests have already written to the table, so a missing TRUNCATE surfaces as a
real failure on a plain ``pytest tests/test_provider_override.py`` run.
"""
from __future__ import annotations

import psycopg
import pytest

from providers import active
from review_queue import store
from scripts.deploy import _resolved_provider

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_set_then_get_returns_the_override():
    store.set_provider_override("groq", T0)
    assert store.get_provider_override() == "groq"


def test_setting_twice_replaces_rather_than_inserting(db_query):
    store.set_provider_override("groq", T0)
    store.set_provider_override("gemini", T1)
    assert store.get_provider_override() == "gemini"
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_restores_none():
    store.set_provider_override("groq", T0)
    store.set_provider_override(None, T1)
    assert store.get_provider_override() is None


def test_a_second_row_is_rejected(db_exec):
    """The singleton CHECK is what makes 'which row wins' unambiguous."""
    store.set_provider_override("groq", T0)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_exec(
            "INSERT INTO runtime_config (id, provider, updated_at) "
            "VALUES (2, 'gemini', %s)",
            (T1,),
        )


def test_an_empty_provider_string_reads_as_no_override(db_exec):
    """The `or None` collapses '' to None -- a blank row must not be treated
    as an override of the empty-string provider."""
    db_exec(
        "INSERT INTO runtime_config (id, provider, updated_at) VALUES (1, '', %s)",
        (T0,),
    )
    assert store.get_provider_override() is None


def test_resolved_provider_matches_the_store_across_row_states(db_exec):
    """There are two implementations of "what is the override": the store's
    pooled, dict_row read (used by the dispatcher) and scripts/deploy.py's
    raw-connection read (used by the CLI's `provider` check and the
    --sync-env masking guard). Their equivalence was previously asserted only
    in a docstring -- if the two ever disagreed, the CLI could report a
    provider check nothing like what the dispatcher is actually running.
    Runs both against the same rows: no row, a set provider, NULL, and an
    empty string."""

    def resolved() -> str | None:
        return _resolved_provider()[1]

    # no row at all
    assert store.get_provider_override() is None
    assert resolved() is None

    # a set provider
    store.set_provider_override("groq", T0)
    assert store.get_provider_override() == "groq"
    assert resolved() == "groq"

    # NULL
    db_exec("UPDATE runtime_config SET provider = NULL WHERE id = 1")
    assert store.get_provider_override() is None
    assert resolved() is None

    # empty string
    db_exec("UPDATE runtime_config SET provider = '' WHERE id = 1")
    assert store.get_provider_override() is None
    assert resolved() is None


def test_override_defaults_to_none():
    assert store.get_provider_override() is None


@pytest.fixture(autouse=True)
def _clean_cache():
    active.reset_override_cache()
    yield
    active.reset_override_cache()


def test_active_provider_reflects_the_cached_value():
    active.set_override_cache("groq")
    assert active.active_provider() == "groq"


def test_clearing_the_cache_returns_to_unconfigured():
    active.set_override_cache("groq")
    active.set_override_cache(None)
    assert active.active_provider() == ""
