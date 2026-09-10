"""Durable ticket store (Postgres via psycopg3) — the queue's source of truth.

One row per (repo, pr): UNIQUE collapses re-triggers so a new push updates the
existing ticket's head_sha instead of stacking a duplicate. A ticket's persisted
``not_before`` prevents an early run after a restart. Timestamps are ISO-8601 UTC
TEXT (lexical comparison == chronological). Functions are synchronous; async
callers wrap them in asyncio.to_thread so Postgres network latency never blocks
the event loop.

Also owns the ``reviews`` table — an insert-only history of completed reviews
(provider, model, timing, tokens, cost, findings) that has no bearing on queue
lifecycle but backs the ``GET /`` / ``GET /api/dashboard`` ops/demo
page (``dashboard/router.py``) via the ``dashboard_*`` read helpers below.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from config import settings
from providers import registry
from review_queue import cooldown_config, runtime_config_defaults
from specialists.schemas import ReviewResult

logger = logging.getLogger(__name__)

# (name, SQL type + constraints) for every runtime_config column, in DDL
# order. The single source of truth for that table's shape: _SCHEMA below
# builds its CREATE TABLE from this, and scripts/deploy.py's
# check_runtime_config_schema() reads the same names to verify the live
# database actually has them all. CREATE TABLE IF NOT EXISTS is a no-op
# against a table that already exists, so a column added here after first
# boot never reaches an already-provisioned database on its own -- that gap
# is exactly what left a real deployment's runtime_config missing
# review_draft_prs (added after this table was first provisioned there) and
# is why deploy.py carries a check for it rather than relying on this file
# alone.
RUNTIME_CONFIG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1)"),
    ("provider", "TEXT"),
    ("updated_at", "TEXT NOT NULL"),
    ("cooldown_base_seconds", "DOUBLE PRECISION"),
    ("cooldown_max_seconds", "DOUBLE PRECISION"),
    ("cooldown_factor", "DOUBLE PRECISION"),
    ("gemini_key_index", "INTEGER"),
    ("groq_key_index", "INTEGER"),
    ("vertex_key_index", "INTEGER"),
    ("key_usage_token_cap", "INTEGER"),
    ("key_usage_reset_time_utc", "TEXT"),
    ("review_draft_prs", "BOOLEAN"),
    ("llm_request_timeout_seconds", "DOUBLE PRECISION"),
    ("dispatcher_default_retry_after_seconds", "DOUBLE PRECISION"),
    ("dispatcher_failure_base_backoff_seconds", "DOUBLE PRECISION"),
    ("dispatcher_failure_max_backoff_seconds", "DOUBLE PRECISION"),
    ("dispatcher_max_failure_attempts", "INTEGER"),
    ("dispatcher_max_notice_post_attempts", "INTEGER"),
    ("dispatcher_min_retry_after_seconds", "DOUBLE PRECISION"),
    ("dispatcher_backoff_jitter_seconds", "DOUBLE PRECISION"),
    ("dispatcher_notice_sweep_batch_size", "INTEGER"),
    ("dispatcher_idle_sleep_seconds", "DOUBLE PRECISION"),
)

# (name, SQL type + constraints) for every slot_config column, in DDL order
# -- the same single-source-of-truth shape RUNTIME_CONFIG_COLUMNS above has,
# and for the same reason: init_pool() widens a live table from this tuple
# with ADD COLUMN IF NOT EXISTS, and get_slot_config()/set_slot_config()
# name these columns explicitly, so a table narrower than this raises
# UndefinedColumn from the bot's own read path.
SLOT_CONFIG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("provider", "TEXT    NOT NULL"),
    ("slot_index", "INTEGER NOT NULL"),
    ("model", "TEXT"),
    ("vertex_gcp_project", "TEXT"),
    ("vertex_gcp_location", "TEXT"),
    ("updated_at", "TEXT    NOT NULL"),
)

# Declared, not migrated: this is the final shape, provisioned in one pass on
# first boot. No column/type ALTER statements -- a fresh clone carries no
# migration code (design spec 2026-08-18 section 6d), and an existing
# database is recreated out of band rather than migrated in place (section
# 9). ENABLE ROW LEVEL SECURITY is the one ALTER TABLE exception: it's
# idempotent (a no-op when already enabled, never an error) and declarative
# the same way CREATE TABLE IF NOT EXISTS/CREATE INDEX IF NOT EXISTS already
# are, not a column-shape migration.
#
# No policies are created alongside it. This project's own connection
# (settings.database_url) is these tables' owner -- Postgres exempts a
# table's owner from RLS by default (FORCE ROW LEVEL SECURITY would remove
# that exemption, and is deliberately not used here), so the bot's own
# queries are completely unaffected. What RLS-with-no-policies actually
# does is deny every row to any *other* role with no BYPASSRLS attribute --
# concretely, Supabase's PostgREST anon/authenticated roles, which this
# project never uses (no supabase-py, no REST calls, DATABASE_URL is a
# direct Postgres connection) but which Supabase still exposes publicly
# by default on every project regardless. Verified
# live against Supabase's own Management API OpenAPI schema
# (api.supabase.com/api/v1-json) that POST /v1/projects has no field for
# this; Supabase's Studio dashboard has an "auto-enable RLS" project setting,
# but it only fires for tables created through Studio's own Table Editor UI,
# never for tables created via raw SQL like these -- so enabling it here,
# in the DDL that actually creates these tables, is the only mechanism that
# reaches them at all.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    head_sha           TEXT,
    status             TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    not_before         TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    comment_id         BIGINT,
    enqueued_at        TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    rereview_requested INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at   TEXT,
    cooldown_level     INTEGER NOT NULL DEFAULT 0,
    notice_not_before  TEXT,
    last_error         TEXT,
    defer_reason       TEXT,
    UNIQUE (repo_full_name, pr_number)
);
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
CREATE TABLE IF NOT EXISTS runtime_config (
""" + ",\n".join(
    f"    {name:<25} {sql_type}" for name, sql_type in RUNTIME_CONFIG_COLUMNS
) + """
);
ALTER TABLE runtime_config ENABLE ROW LEVEL SECURITY;
CREATE TABLE IF NOT EXISTS slot_config (
""" + ",\n".join(
    f"    {name:<25} {sql_type}" for name, sql_type in SLOT_CONFIG_COLUMNS
) + """,
    PRIMARY KEY (provider, slot_index)
);
ALTER TABLE slot_config ENABLE ROW LEVEL SECURITY;
CREATE TABLE IF NOT EXISTS reviews (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    comment_id         BIGINT,
    created_at         TEXT    NOT NULL,
    total_elapsed_ms   INTEGER NOT NULL,
    total_tokens_in    INTEGER NOT NULL,
    total_tokens_out   INTEGER NOT NULL,
    est_cost_usd       DOUBLE PRECISION,  -- NULL when the model has no rate entry
    results            JSONB   NOT NULL,
    key_index          INTEGER
);
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS reviews_created_at_idx ON reviews (created_at DESC);
"""

_WIDENED_TABLES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("runtime_config", RUNTIME_CONFIG_COLUMNS),
    ("slot_config", SLOT_CONFIG_COLUMNS),
)


def _widen_statements(conn) -> list[str]:
    """`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for every declared column
    the live tables lack, in declared order.

    CREATE TABLE IF NOT EXISTS is a no-op against a table that already
    exists, so a column added to this file after a database was provisioned
    -- by an onboarding wizard, by an operator, by an older release of this
    service -- never reaches it on its own. That gap is what left a real
    deployment's runtime_config missing review_draft_prs.

    ADD COLUMN IF NOT EXISTS only ever ADDs, and is idempotent and
    declarative in exactly the way ENABLE ROW LEVEL SECURITY already is
    above -- not a column-shape migration, which this schema still does not
    do (see RUNTIME_CONFIG_COLUMNS's "Declared, not migrated" note). No
    ALTER COLUMN, no DROP COLUMN, no type change, ever.
    """
    statements: list[str] = []
    for table, columns in _WIDENED_TABLES:
        live = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            ).fetchall()
        }
        by_name = dict(columns)
        statements.extend(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {by_name[name]};"
            for name, _sql_type in columns
            if name not in live
        )
    return statements


def _backfill_runtime_config(conn) -> list[str]:
    """Fill every runtime_config column that is NULL with this repo's
    declared default, and return the names of the ones actually filled.

    NOT the seeding that was removed in 2026-09-09. That one used
    `INSERT ... ON CONFLICT (id) DO NOTHING`, which asks "does row 1
    exist?" and skips EVERY column when it does -- so a provisioning step
    that created the row first for provider/key_index left the other 18
    columns permanently NULL, and every PR review on that deployment stuck
    behind a "Dispatcher configuration issue" comment forever (ISSUES.md
    2026-09-09, both repos).

    `DO UPDATE SET col = COALESCE(runtime_config.col, EXCLUDED.col)` asks
    the right question per column instead: it creates the row when absent,
    fills only NULLs when present, and can never overwrite a value the
    onboarding wizard, scripts/deploy.py --sync-config-db, or the
    dashboard's config panel deliberately wrote.

    A column whose declared default is None is omitted entirely rather than
    COALESCE'd against NULL -- see runtime_config_defaults.NO_DEFAULT_BY_DESIGN.
    """
    defaults = runtime_config_defaults.declared_defaults()
    columns = tuple(defaults)
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM runtime_config WHERE id = 1"
    ).fetchone()
    if row is None:
        # No row at all: the INSERT below creates the whole thing. Reported
        # as every column rather than none, so "nothing provisioned this
        # database" is the loudest case in the log rather than the quietest.
        was_null = list(columns)
    else:
        was_null = [name for name, value in row.items() if value is None]
    placeholders = ", ".join(["%s"] * len(columns))
    assignments = ", ".join(
        f"{c} = COALESCE(runtime_config.{c}, EXCLUDED.{c})" for c in columns
    )
    conn.execute(
        f"INSERT INTO runtime_config (id, {', '.join(columns)}, updated_at) "
        f"VALUES (1, {placeholders}, %s) "
        f"ON CONFLICT (id) DO UPDATE SET {assignments}",
        (*(defaults[c] for c in columns), datetime.now(timezone.utc).isoformat()),
    )
    return was_null


_pool: ConnectionPool | None = None

# Explicit rather than relying on psycopg_pool's default (same value), so a test
# can shrink it without waiting 30s for a connection that will never open.
_POOL_TIMEOUT_SECONDS = 30

_FIRST_CONNECT_HELP = (
    "could not open a Postgres connection at startup within {timeout:.0f}s. "
    "On a first deploy this is nearly always one of:\n"
    "  1. the database is still provisioning -- wait until it reports ready, "
    "then deploy again (a failed deploy is not retried automatically);\n"
    "  2. a pooler connection string whose username is missing its project "
    "suffix -- it must look like postgres.<project-ref>, not plain postgres;\n"
    "  3. a password containing characters that must be percent-encoded "
    "(@ # / ?).\n"
    "The driver's own error is logged above as \"error connecting in 'pool-1'\"."
)


@dataclass
class Ticket:
    id: int
    repo_full_name: str
    pr_number: int
    head_sha: str | None
    status: str
    provider: str
    not_before: str | None
    attempts: int
    comment_id: int | None
    enqueued_at: str
    updated_at: str
    rereview_requested: int
    last_reviewed_at: str | None
    cooldown_level: int
    notice_not_before: str | None
    last_error: str | None
    # Why this ticket is deferred: NULL/'provider' = a provider rate limit or
    # a re-review cooldown (today's only meaning); 'usage_cap' = the bot's own
    # per-key daily cap. Drives which wording the PR notice uses. Only
    # meaningful while status == 'deferred' -- a row in any other status
    # (e.g. 'retrying' or 'done') may carry a stale leftover value that no
    # code path reads.
    defer_reason: str | None


def _configure(conn) -> None:
    conn.row_factory = dict_row


def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema. Idempotent.

    A PoolTimeout here means the very first connection never succeeded, which on a
    hosted first deploy is nearly always a provisioning or connection-string
    problem rather than a transient blip. Re-raise it as a RuntimeError carrying
    the likely causes: the bare PoolTimeout reads like a hang, and the driver's
    real error is tens of lines further up the log. Startup still fails loudly
    (design spec section 11) -- RuntimeError matches _require_pool()'s convention
    and main.py's lifespan already documents it as the fail-loudly path. The
    message never includes settings.database_url, which carries the password.

    Backfills every NULL runtime_config column with this repo's OWN declared
    default via COALESCE -- this is NOT the seeding removed in 2026-09-09.
    That one used `INSERT ... ON CONFLICT (id) DO NOTHING`, which asks "does
    row 1 exist?" and skips EVERY column when it does -- so a provisioning
    step that created the row first for provider/key_index left the other 18
    columns permanently NULL, and every PR review on that deployment stuck
    behind a "Dispatcher configuration issue" comment forever (ISSUES.md
    2026-09-09, both repos). `DO UPDATE SET col = COALESCE(runtime_config.col,
    EXCLUDED.col)` asks the right question per column instead, and can never
    overwrite a value someone else wrote -- see _backfill_runtime_config()'s
    own docstring for the full mechanism.

    What this function still will NOT invent: `provider` and the matching
    `slot_config` row. Those are the wizard's/operator's own unique promise,
    not something this repo could derive -- main.py's lifespan still fails
    loudly (RuntimeError) if either is missing, exactly as before.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=4,
            timeout=_POOL_TIMEOUT_SECONDS,
            configure=_configure,
            open=True,
        )
    try:
        with _pool.connection() as conn:
            conn.execute(_SCHEMA)
            for statement in _widen_statements(conn):
                conn.execute(statement)
            filled = _backfill_runtime_config(conn)
            if filled:
                logger.warning(
                    "runtime_config was incomplete at boot; filled %d column(s) "
                    "with this release's declared defaults: %s. Whoever "
                    "provisioned this database should write a complete row "
                    "(`uv run python -m scripts.deploy --sync-config-db`).",
                    len(filled),
                    ", ".join(filled),
                )
    except PoolTimeout as exc:
        raise RuntimeError(
            _FIRST_CONNECT_HELP.format(timeout=_POOL_TIMEOUT_SECONDS)
        ) from exc


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _require_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("store.init_pool() has not been called")
    return _pool


def _row_to_ticket(row: dict) -> Ticket:
    return Ticket(**row)


_MAX_COOLDOWN_LEVEL = 30


def effective_cooldown(level: int) -> float:
    """Escalated per-PR cooldown: min(base * factor^min(level, _MAX_COOLDOWN_LEVEL), cap).

    level 0 -> base (identical to a non-escalating cooldown, so normal PRs are
    unaffected). Each consecutive rapid re-review raises the level, geometrically
    lengthening the next wait, capped at the effective cap. base/cap/factor come
    from cooldown_config.effective_config() -- a DB override when set and valid,
    else the env-configured defaults.
    """
    base, cap, factor = cooldown_config.effective_config()
    return max(base, min(base * factor ** min(level, _MAX_COOLDOWN_LEVEL), cap))


def next_cooldown_level(level: int) -> int:
    """Level for the next re-review after a churn re-review (guarded against overflow)."""
    return min(level + 1, _MAX_COOLDOWN_LEVEL)


def usage_bucket_start(now: datetime, reset_time: time) -> datetime:
    """UTC instant the current usage window began.

    If ``now``'s UTC time-of-day is before ``reset_time``, the window started
    at *yesterday's* reset_time; otherwise today's. The boundary instant
    itself belongs to the NEW window, so a review landing exactly on it is
    accounted to the fresh day.

    Pure function of (now, reset_time) -- no DB state -- and colocated here
    with effective_cooldown/next_cooldown_level, the existing precedent for
    small pure helpers living beside the module that calls them.

    Precondition: ``now`` must already be UTC-aware (e.g. ``datetime.now(timezone.utc)``,
    as every actual caller passes) -- a naive or non-UTC-aware value is read as
    wall-clock components and silently mis-bucketed.
    """
    candidate = datetime.combine(now.date(), reset_time, tzinfo=timezone.utc)
    if now.time() < reset_time:
        candidate -= timedelta(days=1)
    return candidate


def _due_after_cooldown(
    last_reviewed_at: str | None, now: str, level: int
) -> tuple[str, str | None, int]:
    """Re-arm state + next escalation level, honoring the escalating cooldown.

    Churn (within effective_cooldown(level) of last completed review):
      → ('deferred', due, next_cooldown_level(level)).
    Quiet or never-reviewed:
      → ('pending', None, 0); escalation resets.
    """
    if last_reviewed_at is None:
        return ("pending", None, 0)
    due = datetime.fromisoformat(last_reviewed_at) + timedelta(seconds=effective_cooldown(level))
    if datetime.fromisoformat(now) < due:
        return ("deferred", due.isoformat(), next_cooldown_level(level))
    return ("pending", None, 0)


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Enqueue/update a ticket under the per-state re-review policy. The whole
    read-branch-write runs in one transaction; SELECT ... FOR UPDATE locks the
    row so a concurrent writer cannot interleave (Postgres analogue of the old
    SQLite BEGIN IMMEDIATE). The done/failed re-arm branch also escalates
    `cooldown_level` on churn (a push arriving before the current cooldown
    elapses) and resets it to 0 once a PR goes quiet for a full window --
    see `effective_cooldown`/`next_cooldown_level`."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE repo_full_name = %s AND pr_number = %s FOR UPDATE",
            (repo_full_name, pr_number),
        ).fetchone()
        if row is None:
            inserted = conn.execute(
                """
                INSERT INTO tickets
                  (repo_full_name, pr_number, head_sha, status, provider, not_before,
                   attempts, comment_id, enqueued_at, updated_at, rereview_requested,
                   last_reviewed_at, cooldown_level, notice_not_before)
                VALUES (%s, %s, %s, 'pending', %s, NULL, 0, NULL, %s, %s, 0, NULL, 0, NULL)
                ON CONFLICT (repo_full_name, pr_number) DO NOTHING
                RETURNING id
                """,
                (repo_full_name, pr_number, head_sha, provider, now, now),
            ).fetchone()
            if inserted is not None:
                return int(inserted["id"])
            # A concurrent insert won the race; block on its lock, then read it.
            row = conn.execute(
                "SELECT * FROM tickets WHERE repo_full_name = %s AND pr_number = %s FOR UPDATE",
                (repo_full_name, pr_number),
            ).fetchone()

        status = row["status"]
        ticket_id = int(row["id"])
        if status == "running":
            conn.execute(
                "UPDATE tickets SET head_sha = %s, rereview_requested = 1, updated_at = %s "
                "WHERE id = %s",
                (head_sha, now, ticket_id),
            )
        elif status in ("pending", "deferred", "retrying"):
            conn.execute(
                "UPDATE tickets SET head_sha = %s, updated_at = %s WHERE id = %s",
                (head_sha, now, ticket_id),
            )
        else:  # 'done'/'failed' -> re-arm honoring the escalating cooldown
            new_status, not_before, new_level = _due_after_cooldown(
                row["last_reviewed_at"], now, row["cooldown_level"]
            )
            conn.execute(
                "UPDATE tickets SET head_sha = %s, status = %s, not_before = %s, attempts = 0, "
                "rereview_requested = 0, cooldown_level = %s, defer_reason = NULL, "
                "updated_at = %s WHERE id = %s",
                (head_sha, new_status, not_before, new_level, now, ticket_id),
            )
        return ticket_id


def claim_next_due(now: str) -> Ticket | None:
    """Atomically claim the oldest due ticket via FOR UPDATE SKIP LOCKED."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE tickets SET status = 'running', updated_at = %s, rereview_requested = 0
            WHERE id = (
                SELECT id FROM tickets
                WHERE status = 'pending'
                   OR (status IN ('deferred','retrying') AND not_before IS NOT NULL
                       AND not_before <= %s)
                ORDER BY enqueued_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (now, now),
        ).fetchone()
        return _row_to_ticket(row) if row else None


def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    """Per-provider rate-limit deferral. Does NOT count toward the hard stop.

    Explicitly clears defer_reason: a stale 'usage_cap' left over from an
    earlier deferral of this same row would mislabel this provider wait as
    the bot's own cap in the PR notice.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = %s, "
            "defer_reason = NULL, updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


def defer_usage_capped(ticket_id: int, not_before: str, now: str) -> None:
    """Defer until the bot's own per-key daily usage cap resets.

    The ONLY writer of defer_reason='usage_cap'. Like defer_rate_limited this
    does NOT touch `attempts` -- a self-imposed wait is not a failure and must
    never count toward the hard stop.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = %s, "
            "defer_reason = 'usage_cap', updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    """Per-ticket hard-failure backoff. Sets status='retrying' (distinct from a
    cooldown/rate-limit 'deferred' wait, so a schedule notice never posts on a
    ticket that's actually silently retrying after an error) and increments
    attempts (drives backoff + hard stop)."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'retrying', not_before = %s, "
            "attempts = attempts + 1, updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


def finalize_review(
    ticket_id: int,
    now: str,
    rereview_not_before: str,
    rereview_cooldown_level: int,
    comment_id: int | None = None,
) -> None:
    """Finalize a completed review, resolving the dirty flag in one statement.

    Always records last_reviewed_at + comment_id. If a push set rereview_requested
    during the run, re-arm to 'deferred' at rereview_not_before with a fresh
    attempts budget and store the escalated rereview_cooldown_level; otherwise mark
    'done' and leave the level unchanged (latent — the next push resolves it).
    """
    with _require_pool().connection() as conn:
        conn.execute(
            """
            UPDATE tickets SET
              last_reviewed_at   = %(now)s,
              comment_id         = COALESCE(%(comment_id)s, comment_id),
              status             = CASE WHEN rereview_requested = 1 THEN 'deferred' ELSE 'done' END,
              not_before         = CASE WHEN rereview_requested = 1 THEN %(rnb)s ELSE NULL END,
              attempts           = CASE WHEN rereview_requested = 1 THEN 0 ELSE attempts END,
              cooldown_level     = CASE WHEN rereview_requested = 1
                                        THEN %(new_level)s ELSE cooldown_level END,
              rereview_requested = 0,
              defer_reason       = NULL,
              updated_at         = %(now)s
            WHERE id = %(id)s
            """,
            {
                "now": now,
                "comment_id": comment_id,
                "rnb": rereview_not_before,
                "new_level": rereview_cooldown_level,
                "id": ticket_id,
            },
        )


def set_comment_id(ticket_id: int, comment_id: int | None) -> None:
    """Persist a comment id discovered on any route that touches the bot's
    comment (placeholder, schedule notice, footnote, clear), independent of
    whatever status transition that route also makes. No-op when comment_id
    is None -- callers pass whatever a github_app call returned, and a
    caller with nothing new to report (e.g. no comment existed to touch)
    must never erase an already-persisted id."""
    if comment_id is None:
        return
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET comment_id = %s WHERE id = %s", (comment_id, ticket_id)
        )


def clear_visible_review(ticket_id: int) -> None:
    """Mark a ticket's review as no longer visible on GitHub: its comment was
    confirmed gone (github_app had to create a fresh one for a footnote/notice
    rather than edit the one on file) so the content that lived in it is
    unrecoverable. Nulls last_reviewed_at so _has_visible_review-driven
    decisions -- cooldown re-arm timing, placeholder-vs-footnote choice --
    reflect reality instead of a stale success record."""
    with _require_pool().connection() as conn:
        conn.execute("UPDATE tickets SET last_reviewed_at = NULL WHERE id = %s", (ticket_id,))


def cancel_ticket(*, repo_full_name: str, pr_number: int, now: str) -> Ticket | None:
    """Cancel a queued-but-not-yet-claimed ticket when its PR closes or
    merges, so it's never picked up and never wastes an LLM call / posts a
    stale comment. No-op if no ticket exists, or if it's already 'running'
    (an in-flight attempt has already committed its spend and posted its
    comment -- see dispatcher.py's module docstring on why there's no cheap
    way to abort it mid-flight) or already terminal ('done'/'failed'/
    'cancelled'). A later `reopened` push revives it via enqueue_or_update's
    existing terminal-state re-arm branch -- 'cancelled' falls into the same
    catch-all as 'done'/'failed'.

    Returns the cancelled ticket (pre-update field values except status/
    updated_at), or None if nothing matched -- so a caller can tell whether a
    schedule-notice footnote needs stripping from GitHub: a cancelled ticket
    is never claimed again, so it's the caller's only chance (see
    webhook.py's _CANCEL_ACTIONS handling)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "UPDATE tickets SET status = 'cancelled', updated_at = %s "
            "WHERE repo_full_name = %s AND pr_number = %s "
            "AND status IN ('pending', 'deferred', 'retrying') "
            "RETURNING *",
            (now, repo_full_name, pr_number),
        ).fetchone()
        return _row_to_ticket(row) if row else None


def discard_skipped_ticket(ticket_id: int, now: str) -> None:
    """Roll back a ticket claimed for a review that turned out to need no
    review at all (orchestrator.ReviewSkipped: an empty diff, or a draft PR
    that review_draft_config says not to review): no comment was ever posted
    and nothing was reviewed, so the ticket must leave no trace of this run
    -- unlike a normal completion, calling finalize_review here would wrongly
    stamp last_reviewed_at/comment_id and make a nonexistent review look
    "visible" to later preservation logic (dispatcher's _has_visible_review).

    Three outcomes, in priority order:
    1. A push landed on the same PR while this ticket was being processed
       (rereview_requested, set by a concurrent enqueue_or_update) -- that
       push may carry real content, so it must not be lost: reset to
       'pending' (immediately due) regardless of anything below.
    2. The ticket was never reviewed before (last_reviewed_at is NULL) --
       nothing to preserve, so the row is deleted outright, leaving no
       trace.
    3. The ticket WAS reviewed before -- revert to 'done', preserving
       last_reviewed_at/comment_id/cooldown_level untouched, rather than
       deleting. Deleting here would reset the re-review cooldown escalation
       for a PR that's already been flagged as churny, which a dummy
       empty-diff/draft push could otherwise be used to exploit on purpose.

    No-op if the ticket no longer exists.
    """
    with _require_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM tickets WHERE id = %s AND rereview_requested = 0 "
            "AND last_reviewed_at IS NULL",
            (ticket_id,),
        )
        if cur.rowcount > 0:
            return
        cur = conn.execute(
            "UPDATE tickets SET status = 'pending', not_before = NULL, attempts = 0, "
            "rereview_requested = 0, defer_reason = NULL, updated_at = %s "
            "WHERE id = %s AND rereview_requested = 1",
            (now, ticket_id),
        )
        if cur.rowcount > 0:
            return
        conn.execute(
            "UPDATE tickets SET status = 'done', not_before = NULL, "
            "rereview_requested = 0, defer_reason = NULL, updated_at = %s "
            "WHERE id = %s AND last_reviewed_at IS NOT NULL",
            (now, ticket_id),
        )


def migrate_repo_rename(old_full_name: str, new_full_name: str, now: str) -> None:
    """Rewrite every tickets/reviews row's repo_full_name from old to new.

    Detected when a GitHub call resolves a stored name to a different
    canonical one -- the repo was renamed, and GitHub transparently redirects
    old-name requests rather than erroring, so there's no exception to react
    to (see orchestrator.attempt_review, the sole caller).

    Guarded against tickets' UNIQUE (repo_full_name, pr_number): a fresh
    webhook under the new name may have already created a ticket for the
    same PR before this migration runs. Any such colliding old-named row is
    cancelled instead of migrated -- it's superseded by the row already
    tracking that PR under the new name, and cancel_ticket's own semantics
    (a no-op against 'running'/terminal rows) apply here too. reviews has no
    such constraint -- it's insert-only history -- so its rows always move.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            """
            UPDATE tickets SET repo_full_name = %(new)s, updated_at = %(now)s
            WHERE repo_full_name = %(old)s
              AND NOT EXISTS (
                SELECT 1 FROM tickets t2
                WHERE t2.repo_full_name = %(new)s AND t2.pr_number = tickets.pr_number
              )
            """,
            {"old": old_full_name, "new": new_full_name, "now": now},
        )
        conn.execute(
            "UPDATE tickets SET status = 'cancelled', updated_at = %s "
            "WHERE repo_full_name = %s AND status IN ('pending', 'deferred', 'retrying')",
            (now, old_full_name),
        )
        conn.execute(
            "UPDATE reviews SET repo_full_name = %s WHERE repo_full_name = %s",
            (new_full_name, old_full_name),
        )


def mark_failed(ticket_id: int, now: str, error: str | None = None) -> None:
    """Mark a ticket as failed after a non-rate-limit exception from attempt_review.

    A push to a 'failed' (or 'done') ticket is handled by
    ``enqueue_or_update``'s terminal-state branch: it calls
    ``_due_after_cooldown`` and re-arms the ticket to 'pending' (cooldown
    elapsed, or no prior successful review) or 'deferred' (still cooling down
    from the last completed review), escalating/resetting ``cooldown_level``
    per the escalation policy and resetting ``attempts`` to 0 either way.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'failed', last_error = %s, updated_at = %s WHERE id = %s",
            (error, now, ticket_id),
        )


def record_review(
    repo_full_name: str,
    pr_number: int,
    review: ReviewResult,
    comment_id: int | None,
    now: str,
    key_index: int,
) -> None:
    """Persist a completed review for the dashboard (insert-only).

    ``key_index`` is the API-key slot that actually paid for this review; it
    is what get_key_usage() sums over, so the per-slot daily cap can be
    scoped to one credential. Deliberately has NO default: silently
    attributing a review to slot 0 would corrupt exactly the accounting the
    cap depends on. Rows written before this column existed are NULL and are
    read as index 0.

    Callers must never let a failure here affect the review itself -- the PR
    comment is already posted by the time this is called.
    """
    results = Jsonb([r.model_dump() for r in review.results])
    with _require_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO reviews
              (repo_full_name, pr_number, provider, model, comment_id, created_at,
               total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results,
               key_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                repo_full_name, pr_number, review.provider, review.model, comment_id, now,
                review.total_elapsed_ms, review.total_tokens_in, review.total_tokens_out,
                review.est_cost_usd, results, key_index,
            ),
        )


def get_key_usage(provider: str, key_index: int, since: str) -> int:
    """Total tokens recorded for this (provider, key_index) since ``since``
    (inclusive, an ISO-8601 UTC string).

    Tokens only. This used to also return a summed cost, for the removed
    KEY_USAGE_COST_CAP_USD (design spec 2026-08-18 section 6c) -- which left
    it not merely dead but subtly WRONG, since est_cost_usd became nullable
    in the same stage and SUM silently skips NULLs, so the total quietly
    under-reported whenever any review ran on an unpriced model. Tokens come
    straight from the provider's usage response and have no such gap.

    Derived with a SUM over `reviews` rather than kept in a dedicated
    running-total table: at free-tier volume (~20 PRs/day) the aggregate
    costs nothing, and there is no second copy of the number that could
    drift out of sync with the review history it is supposed to describe.
    A NULL key_index (row written before that column existed) counts as
    index 0.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens_in + total_tokens_out), 0) AS tokens
            FROM reviews
            WHERE provider = %s
              AND COALESCE(key_index, 0) = %s
              AND created_at >= %s
            """,
            (provider, key_index, since),
        ).fetchone()
    return int(row["tokens"])


_TICKET_STATUSES = (
    "pending", "running", "deferred", "retrying", "done", "failed", "cancelled"
)


def dashboard_stats() -> dict:
    """Aggregate totals across all recorded reviews (count, cost, avg elapsed)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(est_cost_usd), 0) AS cost, "
            "COALESCE(AVG(total_elapsed_ms), 0) AS avg_ms FROM reviews"
        ).fetchone()
    return {
        "total_reviews": int(row["n"]),
        "total_cost_usd": round(float(row["cost"]), 4),
        "avg_elapsed_ms": int(row["avg_ms"]),
    }


def dashboard_queue_counts() -> dict[str, int]:
    """Ticket counts per status, zero-filled for every known status."""
    counts = {status: 0 for status in _TICKET_STATUSES}
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
        ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts


def dashboard_failed_tickets(limit: int = 100) -> list[dict]:
    """Tickets that exhausted retries (status='failed'), newest first, with
    the exception message (if any) that put them there."""
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT repo_full_name, pr_number, provider, attempts, last_error, updated_at "
            "FROM tickets WHERE status = 'failed' ORDER BY updated_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "repo": row["repo_full_name"],
            "pr_number": row["pr_number"],
            "provider": row["provider"],
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def dashboard_reviews(limit: int = 50) -> list[dict]:
    """Most recent completed reviews, newest first, with a derived comment_url."""
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT repo_full_name, pr_number, provider, model, comment_id, created_at, "
            "total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results "
            "FROM reviews ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    reviews = []
    for row in rows:
        comment_url = None
        if row["comment_id"] is not None:
            comment_url = (
                f"https://github.com/{row['repo_full_name']}/pull/"
                f"{row['pr_number']}#issuecomment-{row['comment_id']}"
            )
        reviews.append({
            "repo": row["repo_full_name"],
            "pr_number": row["pr_number"],
            "provider": row["provider"],
            "model": row["model"],
            "created_at": row["created_at"],
            "elapsed_ms": row["total_elapsed_ms"],
            "tokens_in": row["total_tokens_in"],
            "tokens_out": row["total_tokens_out"],
            "est_cost_usd": row["est_cost_usd"],
            "comment_url": comment_url,
            "specialists": row["results"],
        })
    return reviews


def tickets_needing_notice(now: str, limit: int) -> list[Ticket]:
    """Deferred (schedule-wait, never retry-backoff since 'retrying' is a
    distinct status) tickets with a visible prior review whose schedule has
    changed since the last notice was posted (or none was posted yet).
    Excludes a ticket whose not_before has already passed -- it is about to
    be claimed for a real review, so a "scheduled" note for a time that's
    already gone would be wrong. Capped at `limit` per call so a mass re-arm
    can't stall process_next_due for a whole dispatcher tick; any ticket past
    the cap keeps its stale marker and is picked up by the next call
    (self-healing, no new state).

    `limit` is passed in by the caller (dispatcher.py's post_pending_notices,
    resolved through dispatcher_tuning_config.require_config()) rather than
    read live via a SQL subquery: `LIMIT (SELECT dispatcher_notice_sweep_
    batch_size FROM runtime_config WHERE id = 1)` silently became "no limit"
    whenever that column was NULL (a missing row, or one somehow never
    seeded) -- exactly the unbounded pre-fix behavior config.py's
    `gt=0` constraint on DISPATCHER_NOTICE_SWEEP_BATCH_SIZE exists to
    prevent, and that constraint stopped applying once Settings stopped
    being the runtime source for it. require_config() rejects a NULL or
    non-positive value outright, so a caller that gets this far always has a
    real, positive limit.
    """
    with _require_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'deferred'
              AND not_before IS NOT NULL
              AND not_before > %s
              AND last_reviewed_at IS NOT NULL
              AND (notice_not_before IS NULL OR notice_not_before != not_before)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT %s
            """,
            (now, limit),
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    """Record that a notice reflecting not_before was just posted. A single
    independent UPDATE -- not inside enqueue_or_update's or finalize_review's
    transactions, same pattern as mark_failed."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = %s WHERE id = %s", (not_before, ticket_id)
        )


def clear_notice(ticket_id: int) -> None:
    """Clear the notice marker after the dispatcher has stripped the schedule
    footnote from GitHub (called right after a ticket is claimed)."""
    with _require_pool().connection() as conn:
        conn.execute("UPDATE tickets SET notice_not_before = NULL WHERE id = %s", (ticket_id,))


def recover_on_startup(now: str) -> None:
    """Reset any ticket interrupted mid-review (crash) back to pending, clearing the
    dirty flag (the fresh pending review already covers the latest commit)."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', rereview_requested = 0, updated_at = %s "
            "WHERE status = 'running'",
            (now,),
        )


def get_ticket(ticket_id: int) -> Ticket | None:
    with _require_pool().connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None


def get_provider_override() -> str | None:
    """The provider override in force, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get("provider") or None


def set_provider_override(provider: str | None, now: str) -> None:
    """Set the override, or clear it with provider=None.

    Upserts the singleton row: CHECK (id = 1) makes a second row impossible, so
    there is never ambiguity about which row wins.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config (id, provider, updated_at) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET provider = EXCLUDED.provider, "
            "updated_at = EXCLUDED.updated_at",
            (provider, now),
        )


def get_cooldown_overrides() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) overrides in force, or (None, None, None) when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT cooldown_base_seconds, cooldown_max_seconds, cooldown_factor "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    if row is None:
        return (None, None, None)
    return (row["cooldown_base_seconds"], row["cooldown_max_seconds"], row["cooldown_factor"])


def set_cooldown_override(
    base: float | None, cap: float | None, factor: float | None, now: str
) -> None:
    """Set the (base, cap, factor) override triple, or clear a field with None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. Writes exactly the three values it's given.

    Two callers: scripts/deploy.py::sync_config_db(), which writes the full
    triple from .env.config's resolved Settings values, and
    dashboard/environment.py::_apply_config_patch(), which merges PARTIAL
    input against the current row. Neither may call this without first
    passing the merged result through cooldown_config.problems() -- this
    function does not validate, and an invalid triple stored here is
    discarded whole at read time, deferring every re-review with no
    boot-time signal until main.py's gate catches it on the next restart.
    An earlier version of this docstring claimed a single caller that always
    wrote complete values; that assumption is what left this write path
    unvalidated (see the 2026-09-10 cross-repo-contract-direction design,
    section 4.2).
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config "
            "(id, cooldown_base_seconds, cooldown_max_seconds, cooldown_factor, updated_at) "
            "VALUES (1, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "cooldown_base_seconds = EXCLUDED.cooldown_base_seconds, "
            "cooldown_max_seconds = EXCLUDED.cooldown_max_seconds, "
            "cooldown_factor = EXCLUDED.cooldown_factor, "
            "updated_at = EXCLUDED.updated_at",
            (base, cap, factor, now),
        )


def get_key_index_override(provider: str) -> int | None:
    """The API-key-slot index override for `provider`, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get(column)


def set_key_index_override(provider: str, index: int | None, now: str) -> None:
    """Set the override for `provider`, or clear it with index=None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. `column` is looked up through
    registry.KEY_INDEX_COLUMNS -- a hardcoded whitelist of exactly three
    names -- and never built from `provider` directly; psycopg parameterizes
    values but not column identifiers, so this lookup IS the injection
    guard, not an optimization.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with _require_pool().connection() as conn:
        conn.execute(
            f"INSERT INTO runtime_config (id, {column}, updated_at) VALUES (1, %s, %s) "
            f"ON CONFLICT (id) DO UPDATE SET {column} = EXCLUDED.{column}, "
            "updated_at = EXCLUDED.updated_at",
            (index, now),
        )


def get_all_key_index_overrides() -> dict[str, int]:
    """{provider: index} for every provider with a non-null override.

    One query reading all three columns -- the dispatcher calls this once
    per claimed ticket, not once per provider.
    """
    columns = registry.KEY_INDEX_COLUMNS
    select = ", ".join(columns.values())
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {select} FROM runtime_config WHERE id = 1").fetchone()
    if row is None:
        return {}
    return {
        provider: row[column] for provider, column in columns.items() if row[column] is not None
    }


def get_slot_config(provider: str, slot_index: int) -> dict | None:
    """This slot's durable config (model, and for vertex, project/location),
    or None if never configured. Independent of *_key_index (which slot is
    active) -- see docs/superpowers/specs/2026-09-08-slotted-config-and-db-
    delegation-design.md section 4a."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT model, vertex_gcp_project, vertex_gcp_location "
            "FROM slot_config WHERE provider = %s AND slot_index = %s",
            (provider, slot_index),
        ).fetchone()
    if row is None:
        return None
    return {
        "model": row["model"],
        "vertex_gcp_project": row["vertex_gcp_project"],
        "vertex_gcp_location": row["vertex_gcp_location"],
    }


def set_slot_config(
    provider: str,
    slot_index: int,
    *,
    model: str | None,
    vertex_gcp_project: str | None,
    vertex_gcp_location: str | None,
    now: str,
) -> None:
    """Upsert all three fields together -- never a partial-field update, so
    a caller that only means to change one field must read-then-write the
    other two itself (dashboard/environment.py's apply flow already has the
    current row in hand from validation, so this is never a real gap)."""
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO slot_config "
            "(provider, slot_index, model, vertex_gcp_project, vertex_gcp_location, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (provider, slot_index) DO UPDATE SET "
            "model = EXCLUDED.model, "
            "vertex_gcp_project = EXCLUDED.vertex_gcp_project, "
            "vertex_gcp_location = EXCLUDED.vertex_gcp_location, "
            "updated_at = EXCLUDED.updated_at",
            (provider, slot_index, model, vertex_gcp_project, vertex_gcp_location, now),
        )


def delete_slot_config(provider: str, slot_index: int) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "DELETE FROM slot_config WHERE provider = %s AND slot_index = %s",
            (provider, slot_index),
        )


def get_all_slot_configs() -> dict[tuple[str, int], dict]:
    """Every configured (provider, slot_index) row, for the dispatcher's
    once-per-claimed-ticket refresh -- one query, not one per provider."""
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT provider, slot_index, model, vertex_gcp_project, vertex_gcp_location "
            "FROM slot_config"
        ).fetchall()
    return {
        (row["provider"], row["slot_index"]): {
            "model": row["model"],
            "vertex_gcp_project": row["vertex_gcp_project"],
            "vertex_gcp_location": row["vertex_gcp_location"],
        }
        for row in rows
    }


def get_dispatcher_tuning_config() -> dict:
    """The 9 tuning-knob values in force, plus dispatcher_idle_sleep_seconds
    (10 keys total). All keys always present; a value is None only if the
    singleton row itself doesn't exist yet, or a provisioning step wrote it
    without this column (should never happen once main.py's lifespan boot
    gate has passed -- it refuses to start unless every one of the first 9
    is present and in-range) -- see review_queue/dispatcher_tuning_config.py
    for the no-fallback policy the first 9 feed. dispatcher_idle_sleep_seconds
    is included here purely so
    the dashboard config panel has one editable surface for all 10 DB-only
    knobs; run_forever's own throttled read still goes through
    get_idle_sleep_seconds(), not this function -- dispatcher_tuning_config's
    require_config()/problems() deliberately do NOT validate this key, since
    it isn't read through that cache."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT llm_request_timeout_seconds, dispatcher_default_retry_after_seconds,"
            "    dispatcher_failure_base_backoff_seconds, dispatcher_failure_max_backoff_seconds,"
            "    dispatcher_max_failure_attempts, dispatcher_max_notice_post_attempts,"
            "    dispatcher_min_retry_after_seconds, dispatcher_backoff_jitter_seconds,"
            "    dispatcher_notice_sweep_batch_size, dispatcher_idle_sleep_seconds "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    keys = (
        "llm_request_timeout_seconds", "dispatcher_default_retry_after_seconds",
        "dispatcher_failure_base_backoff_seconds", "dispatcher_failure_max_backoff_seconds",
        "dispatcher_max_failure_attempts", "dispatcher_max_notice_post_attempts",
        "dispatcher_min_retry_after_seconds", "dispatcher_backoff_jitter_seconds",
        "dispatcher_notice_sweep_batch_size", "dispatcher_idle_sleep_seconds",
    )
    if row is None:
        return {k: None for k in keys}
    return {k: row[k] for k in keys}


def set_dispatcher_tuning_config(
    *,
    llm_request_timeout_seconds: float | None,
    dispatcher_default_retry_after_seconds: float | None,
    dispatcher_failure_base_backoff_seconds: float | None,
    dispatcher_failure_max_backoff_seconds: float | None,
    dispatcher_max_failure_attempts: int | None,
    dispatcher_max_notice_post_attempts: int | None,
    dispatcher_min_retry_after_seconds: float | None,
    dispatcher_backoff_jitter_seconds: float | None,
    dispatcher_notice_sweep_batch_size: int | None,
    dispatcher_idle_sleep_seconds: float | None,
    now: str,
) -> None:
    """Write-side counterpart to get_dispatcher_tuning_config -- upserts the
    singleton row, same CHECK (id = 1) guarantee as set_cooldown_override.
    Writes exactly the 10 values it's given; a caller applying a PARTIAL
    update (e.g. the config panel's PATCH endpoint) is responsible for
    reading the current 10 via get_dispatcher_tuning_config() and merging
    first, mirroring set_cooldown_override's own contract."""
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config ("
            "    id, llm_request_timeout_seconds, dispatcher_default_retry_after_seconds,"
            "    dispatcher_failure_base_backoff_seconds, dispatcher_failure_max_backoff_seconds,"
            "    dispatcher_max_failure_attempts, dispatcher_max_notice_post_attempts,"
            "    dispatcher_min_retry_after_seconds, dispatcher_backoff_jitter_seconds,"
            "    dispatcher_notice_sweep_batch_size, dispatcher_idle_sleep_seconds, updated_at"
            ") VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "llm_request_timeout_seconds = EXCLUDED.llm_request_timeout_seconds, "
            "dispatcher_default_retry_after_seconds = "
            "    EXCLUDED.dispatcher_default_retry_after_seconds, "
            "dispatcher_failure_base_backoff_seconds = "
            "    EXCLUDED.dispatcher_failure_base_backoff_seconds, "
            "dispatcher_failure_max_backoff_seconds = "
            "    EXCLUDED.dispatcher_failure_max_backoff_seconds, "
            "dispatcher_max_failure_attempts = EXCLUDED.dispatcher_max_failure_attempts, "
            "dispatcher_max_notice_post_attempts = EXCLUDED.dispatcher_max_notice_post_attempts, "
            "dispatcher_min_retry_after_seconds = EXCLUDED.dispatcher_min_retry_after_seconds, "
            "dispatcher_backoff_jitter_seconds = EXCLUDED.dispatcher_backoff_jitter_seconds, "
            "dispatcher_notice_sweep_batch_size = EXCLUDED.dispatcher_notice_sweep_batch_size, "
            "dispatcher_idle_sleep_seconds = EXCLUDED.dispatcher_idle_sleep_seconds, "
            "updated_at = EXCLUDED.updated_at",
            (
                llm_request_timeout_seconds,
                dispatcher_default_retry_after_seconds,
                dispatcher_failure_base_backoff_seconds,
                dispatcher_failure_max_backoff_seconds,
                dispatcher_max_failure_attempts,
                dispatcher_max_notice_post_attempts,
                dispatcher_min_retry_after_seconds,
                dispatcher_backoff_jitter_seconds,
                dispatcher_notice_sweep_batch_size,
                dispatcher_idle_sleep_seconds,
                now,
            ),
        )


def get_idle_sleep_seconds() -> float | None:
    """DISPATCHER_IDLE_SLEEP_SECONDS's DB value, or None if unset (should not
    happen once seeded -- see get_dispatcher_tuning_config's docstring)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT dispatcher_idle_sleep_seconds FROM runtime_config WHERE id = 1"
        ).fetchone()
    return None if row is None else row["dispatcher_idle_sleep_seconds"]


def get_usage_cap_overrides() -> tuple[int | None, str | None]:
    """(token cap, reset time) overrides, or Nones when unset.

    The reset time comes back as the raw "HH:MM"/"HH:MM:SS" TEXT it was stored
    as; parsing (and rejecting garbage) belongs to
    review_queue/usage_cap_config.py, which is where the fail-safe policy lives.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT key_usage_token_cap, key_usage_reset_time_utc "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    if row is None:
        return (None, None)
    return (
        row["key_usage_token_cap"],
        row["key_usage_reset_time_utc"],
    )


def set_usage_cap_override(tokens: int | None, reset: str | None, now: str) -> None:
    """Set the (token cap, reset time) override pair, or clear a field with
    None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. Writes exactly the two values it's given.

    Two callers: scripts/deploy.py::sync_config_db(), which writes the full
    pair from .env.config's resolved Settings values, and
    dashboard/environment.py::_apply_config_patch(), which merges PARTIAL
    input against the current row. Neither may call this without first
    passing the merged result through usage_cap_config.problems() -- this
    function does not validate, and an invalid pair stored here is
    discarded whole at read time (the cap fails OPEN) until main.py's gate
    catches it on the next restart. An earlier version of this docstring
    claimed a single caller that always wrote complete values; that
    assumption is what left this write path unvalidated (see the
    2026-09-10 cross-repo-contract-direction design, section 4.2).
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config "
            "(id, key_usage_token_cap, key_usage_reset_time_utc, updated_at) "
            "VALUES (1, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "key_usage_token_cap = EXCLUDED.key_usage_token_cap, "
            "key_usage_reset_time_utc = EXCLUDED.key_usage_reset_time_utc, "
            "updated_at = EXCLUDED.updated_at",
            (tokens, reset, now),
        )


def get_review_draft_override() -> bool | None:
    """The draft-PR review override in force, or None when unset (falls back
    to Settings.review_draft_prs).

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT review_draft_prs FROM runtime_config WHERE id = 1"
        ).fetchone()
    return (row or {}).get("review_draft_prs")


def set_review_draft_override(value: bool | None, now: str) -> None:
    """Set the override, or clear it with value=None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config (id, review_draft_prs, updated_at) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET review_draft_prs = EXCLUDED.review_draft_prs, "
            "updated_at = EXCLUDED.updated_at",
            (value, now),
        )
