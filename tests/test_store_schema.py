"""The provisioned schema is declared, not migrated -- review_queue/store.py's
_SCHEMA is a CREATE TABLE declaration of the final shape, with no
column/type-altering ALTER statements (design spec 2026-08-18 section 6d).
These tests lock the column set so the ALTER-folding refactor cannot
silently change it. `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` is a
deliberate, narrow exception to the no-ALTER rule (see _SCHEMA's own
comment) -- it's idempotent and declarative, not a migration -- so the
no-ALTER test below only forbids any OTHER kind of ALTER TABLE statement."""
from __future__ import annotations

from review_queue import store

EXPECTED_COLUMNS = {
    "tickets": {
        "id", "repo_full_name", "pr_number", "head_sha", "status", "provider",
        "not_before", "attempts", "comment_id", "enqueued_at", "updated_at",
        "rereview_requested", "last_reviewed_at", "cooldown_level",
        "notice_not_before", "last_error", "defer_reason",
    },
    "runtime_config": {
        "id", "provider", "updated_at", "cooldown_base_seconds",
        "cooldown_max_seconds", "cooldown_factor", "gemini_key_index",
        "groq_key_index", "vertex_key_index", "key_usage_token_cap",
        "key_usage_reset_time_utc", "review_draft_prs",
        "llm_request_timeout_seconds", "dispatcher_default_retry_after_seconds",
        "dispatcher_failure_base_backoff_seconds",
        "dispatcher_failure_max_backoff_seconds", "dispatcher_max_failure_attempts",
        "dispatcher_max_notice_post_attempts", "dispatcher_min_retry_after_seconds",
        "dispatcher_backoff_jitter_seconds", "dispatcher_notice_sweep_batch_size",
        "dispatcher_idle_sleep_seconds",
    },
    "slot_config": {
        "provider", "slot_index", "model", "vertex_gcp_project",
        "vertex_gcp_location", "updated_at",
    },
    "reviews": {
        "id", "repo_full_name", "pr_number", "provider", "model", "comment_id",
        "created_at", "total_elapsed_ms", "total_tokens_in", "total_tokens_out",
        "est_cost_usd", "results", "key_index",
    },
}


def _columns(db_query, table: str) -> set[str]:
    rows = db_query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    return {r[0] for r in rows}


def test_schema_declares_every_expected_column(db, db_query):
    for table, expected in EXPECTED_COLUMNS.items():
        assert _columns(db_query, table) == expected, f"{table} column set changed"


def test_schema_contains_no_column_altering_statements():
    alter_lines = [
        line.strip() for line in store._SCHEMA.upper().splitlines()
        if line.strip().startswith("ALTER TABLE")
    ]
    non_rls_alters = [
        line for line in alter_lines if not line.endswith("ENABLE ROW LEVEL SECURITY;")
    ]
    assert not non_rls_alters, (
        "_SCHEMA must declare the final column shape via CREATE TABLE only -- a "
        "column/type ALTER is migration code, which a fresh clone must not carry "
        "(spec section 6d). ENABLE ROW LEVEL SECURITY is the one documented "
        "exception."
    )


def test_schema_enables_rls_on_every_table(db, db_query):
    for table in EXPECTED_COLUMNS:
        rows = db_query(
            "SELECT relrowsecurity FROM pg_class WHERE relname = %s", (table,)
        )
        assert rows[0][0] is True, f"{table} must have row level security enabled"


def test_runtime_config_has_no_cost_cap_column(db, db_query):
    assert "key_usage_cost_cap_usd" not in _columns(db_query, "runtime_config")


def test_slot_config_columns_build_the_schema_ddl():
    # Same single-source-of-truth shape runtime_config already has: the
    # CREATE TABLE text is generated from the tuple, so they cannot drift.
    from review_queue.store import SLOT_CONFIG_COLUMNS, _SCHEMA

    assert [name for name, _sql_type in SLOT_CONFIG_COLUMNS] == [
        "provider", "slot_index", "model",
        "vertex_gcp_project", "vertex_gcp_location", "updated_at",
    ]
    for name, sql_type in SLOT_CONFIG_COLUMNS:
        assert f"{name:<25} {sql_type}" in _SCHEMA


def test_every_declared_column_is_nullable_or_defaulted_or_provisioner_written():
    """store.init_pool() widens a live table with ADD COLUMN IF NOT EXISTS,
    which fails against a non-empty table for a NOT NULL column carrying no
    DEFAULT. The provisioner-written columns are exempt because they are
    never absent: whoever creates the row writes them in the same statement.
    """
    from review_queue.store import RUNTIME_CONFIG_COLUMNS, SLOT_CONFIG_COLUMNS

    provisioner_written = {"id", "provider", "slot_index", "updated_at"}
    for columns in (RUNTIME_CONFIG_COLUMNS, SLOT_CONFIG_COLUMNS):
        for name, sql_type in columns:
            if name in provisioner_written:
                continue
            upper = sql_type.upper()
            assert "NOT NULL" not in upper or "DEFAULT" in upper, (
                f"{name} is NOT NULL with no DEFAULT -- ADD COLUMN IF NOT EXISTS "
                "cannot add it to a table that already has rows"
            )


def test_widening_safe_sql_type_relaxes_a_bare_not_null():
    assert store._widening_safe_sql_type("TEXT    NOT NULL") == "TEXT"
    assert store._widening_safe_sql_type("INTEGER NOT NULL") == "INTEGER"


def test_widening_safe_sql_type_leaves_a_defaulted_column_untouched():
    # NOT NULL DEFAULT 0 is safe to ADD to a non-empty table as-is -- every
    # existing row gets the default, not NULL, so there's nothing to relax.
    assert store._widening_safe_sql_type("INTEGER NOT NULL DEFAULT 0") == (
        "INTEGER NOT NULL DEFAULT 0"
    )


def test_widening_safe_sql_type_is_a_no_op_for_an_already_nullable_column():
    assert store._widening_safe_sql_type("DOUBLE PRECISION") == "DOUBLE PRECISION"


def test_est_cost_usd_is_nullable(db, db_query):
    # db_query (not db_exec) is this file's actual raw-query fixture --
    # tests/conftest.py's db_exec only executes/commits, it returns nothing to
    # index into. Rows come back as tuples, matching this file's other tests.
    rows = db_query(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'reviews' AND column_name = 'est_cost_usd'"
    )
    assert rows[0][0] == "YES", (
        "an unpriced review records NULL, not 0.0 -- 0.0 would corrupt the "
        "dashboard's SUM(est_cost_usd) aggregate"
    )
