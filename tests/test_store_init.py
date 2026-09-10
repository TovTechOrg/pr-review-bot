"""init_pool()'s failure path: an unreachable Postgres must fail loudly with an
actionable message, must never leak the connection string, and must not rewrite
errors that are not connection timeouts.

Deliberately does NOT use the shared ``db`` fixture (tests/conftest.py) — these
tests point the store at a dead port (127.0.0.1:1 refuses immediately) and never
need a real database.
"""
from __future__ import annotations

import contextlib

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from config import settings
from review_queue import dispatcher_tuning_config, store

SENTINEL_PASSWORD = "sentinel-pw-must-not-appear"
DEAD_URL = (
    f"postgresql://someuser:{SENTINEL_PASSWORD}@127.0.0.1:1/postgres?connect_timeout=1"
)


@pytest.fixture(autouse=True)
def _closed_pool():
    """No module-level pool before or after; these tests own it entirely."""
    store.close_pool()
    yield
    store.close_pool()


def test_init_pool_unreachable_db_raises_actionable_runtime_error(monkeypatch):
    monkeypatch.setattr(settings, "database_url", DEAD_URL)
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    with pytest.raises(RuntimeError) as excinfo:
        store.init_pool()

    message = str(excinfo.value)
    assert "provisioning" in message
    assert "postgres.<project-ref>" in message
    assert "percent-encode" in message.lower()
    # The driver's own error is preserved as the cause, not discarded.
    assert isinstance(excinfo.value.__cause__, PoolTimeout)


def test_init_pool_error_never_leaks_the_connection_string(monkeypatch):
    """CLAUDE.md: no secret is ever logged. database_url carries the password, so
    the actionable message must describe failure shapes, never interpolate it."""
    monkeypatch.setattr(settings, "database_url", DEAD_URL)
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    with pytest.raises(RuntimeError) as excinfo:
        store.init_pool()

    rendered = str(excinfo.value) + repr(excinfo.value.args)
    assert SENTINEL_PASSWORD not in rendered
    assert DEAD_URL not in rendered


def test_init_pool_does_not_mask_a_non_timeout_failure(monkeypatch):
    """Only PoolTimeout gets the friendly rewrite. A privilege error on
    CREATE TABLE must surface as itself -- reporting it as "still provisioning"
    would send the operator chasing the wrong problem.

    A malformed conninfo cannot be used to test this: ConnectionPool constructs
    fine and the failure still arrives as PoolTimeout (and PoolTimeout subclasses
    psycopg.OperationalError). So inject the failure at the DDL step instead.
    """

    class _FakeConn:
        def execute(self, *args, **kwargs):
            raise psycopg.errors.InsufficientPrivilege(
                "permission denied for schema public"
            )

    class _FakePool:
        def __init__(self, *args, **kwargs):
            pass

        @contextlib.contextmanager
        def connection(self):
            yield _FakeConn()

        def close(self):
            pass

    monkeypatch.setattr(store, "ConnectionPool", _FakePool)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        store.init_pool()


@pytest.mark.db
def test_init_pool_widens_a_narrower_pre_provisioned_runtime_config(
    db_url, db_exec, db_query, monkeypatch
):
    """The wizard-provisioning chronology: something else created the table
    first, narrower than this repo declares. CREATE TABLE IF NOT EXISTS is a
    no-op against it, so init_pool() must ADD the missing columns or the
    bot's own INSERT/SELECT hits UndefinedColumn."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS runtime_config CASCADE")
    db_exec(
        "CREATE TABLE runtime_config ("
        "  id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),"
        "  provider TEXT,"
        "  updated_at TEXT NOT NULL"
        ")"
    )
    db_exec("INSERT INTO runtime_config (id, provider, updated_at) "
            "VALUES (1, 'groq', '2026-09-10T00:00:00+00:00')")

    store.init_pool()
    try:
        live = {
            name
            for (name,) in db_query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'runtime_config'"
            )
        }
    finally:
        store.close_pool()
    declared = {name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS}
    assert declared <= live, f"init_pool did not widen: {sorted(declared - live)}"


@pytest.mark.db
def test_init_pool_widens_a_narrower_pre_provisioned_slot_config(
    db_url, db_exec, monkeypatch
):
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS slot_config CASCADE")
    db_exec(
        "CREATE TABLE slot_config ("
        "  provider TEXT NOT NULL,"
        "  slot_index INTEGER NOT NULL,"
        "  model TEXT,"
        "  updated_at TEXT NOT NULL,"
        "  PRIMARY KEY (provider, slot_index)"
        ")"
    )
    db_exec("INSERT INTO slot_config (provider, slot_index, model, updated_at) "
            "VALUES ('groq', 0, 'llama-3.3-70b-versatile', "
            "'2026-09-10T00:00:00+00:00')")

    store.init_pool()
    try:
        row = store.get_slot_config("groq", 0)
    finally:
        store.close_pool()
    # get_slot_config SELECTs vertex_gcp_project/_location by name -- this
    # would raise UndefinedColumn without the widening.
    assert row["model"] == "llama-3.3-70b-versatile"
    assert row["vertex_gcp_project"] is None


@pytest.mark.db
def test_init_pool_widening_is_idempotent(db_url, db_exec, monkeypatch):
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS runtime_config, slot_config CASCADE")
    store.init_pool()
    store.close_pool()
    store.init_pool()  # second boot: every column already present
    store.close_pool()


@pytest.mark.db
def test_init_pool_backfills_null_columns_from_declared_defaults(
    db_url, db_exec, monkeypatch, caplog
):
    """The 2026-09-09 chronology, fixed: a provisioner writes the row with
    only provider/key_index/updated_at, then this service boots. Every
    tuning column it left NULL must come back filled, so the boot gate
    passes and the dispatcher has a usable config."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS runtime_config CASCADE")
    db_exec(
        "CREATE TABLE runtime_config ("
        "  id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),"
        "  provider TEXT, groq_key_index INTEGER, updated_at TEXT NOT NULL"
        ")"
    )
    db_exec("INSERT INTO runtime_config (id, provider, groq_key_index, updated_at) "
            "VALUES (1, 'groq', 0, '2026-09-10T00:00:00+00:00')")

    with caplog.at_level("WARNING"):
        store.init_pool()
    try:
        config = store.get_dispatcher_tuning_config()
        tokens, reset = store.get_usage_cap_overrides()
        base, cap, factor = store.get_cooldown_overrides()
    finally:
        store.close_pool()

    assert dispatcher_tuning_config.problems(config) == []
    assert (base, cap, factor) == (300.0, 3600.0, 2.0)
    assert reset == "04:00:00"
    # NULL is this column's declared default -- "cap intentionally
    # disabled" is a valid state, so the backfill must leave it alone.
    assert tokens is None
    # Names only, never values -- these are operational settings, but the
    # convention is uniform.
    assert "cooldown_base_seconds" in caplog.text
    assert "key_usage_token_cap" not in caplog.text


@pytest.mark.db
def test_init_pool_backfill_never_overwrites_an_existing_value(db_url, db_exec, monkeypatch):
    """COALESCE, not ON CONFLICT DO NOTHING and not an unconditional write:
    a value a provisioner, the CLI, or the dashboard deliberately set must
    survive every subsequent boot untouched."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS runtime_config CASCADE")
    store.init_pool()
    store.set_cooldown_override(11.0, 22.0, 3.0, "2026-09-10T00:00:00+00:00")
    store.close_pool()

    store.init_pool()
    try:
        assert store.get_cooldown_overrides() == (11.0, 22.0, 3.0)
    finally:
        store.close_pool()


@pytest.mark.db
def test_init_pool_backfill_creates_the_row_when_absent(
    db_url, db_exec, monkeypatch, caplog
):
    """No provisioner ran at all: the row must be created with defaults, so
    the only thing main.py's gate then complains about is `provider`, which
    the bot genuinely cannot invent. This is the loudest case, not the
    quietest -- every backfillable column is reported as filled."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS runtime_config CASCADE")
    with caplog.at_level("WARNING"):
        store.init_pool()
    assert "cooldown_base_seconds" in caplog.text
    assert "dispatcher_notice_sweep_batch_size" in caplog.text
    try:
        assert dispatcher_tuning_config.problems(
            store.get_dispatcher_tuning_config()
        ) == []
        assert store.get_provider_override() is None
    finally:
        store.close_pool()
