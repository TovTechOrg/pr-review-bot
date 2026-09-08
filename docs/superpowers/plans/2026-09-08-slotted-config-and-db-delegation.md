# Slotted Vertex Config + DB Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each provider's model (and Vertex's project/location) resolvable per credential slot, and move 9 dispatcher/timeout tuning knobs off Render entirely — all DB-only (`runtime_config` + a new `slot_config` table), no env-var fallback anywhere, a missing value is a real visible failure.

**Architecture:** `providers/factory.py::get_provider()` already resolves the active slot index *before* building a provider — this plan slots into that exact join point. Every "DB is sole source of truth" value follows the exact pattern `review_queue/cooldown_config.py` already established (module-level cache dict, refreshed via `asyncio.to_thread` once per claimed ticket in `dispatcher.py::process_next_due`, pushed in via `set_override_cache`) — this plan removes that pattern's *env-fallback* branch everywhere it appears (existing and new), and adds one new table (`slot_config`) for the per-slot dimension a singleton `runtime_config` row can't represent.

**Tech Stack:** Python 3.12, pytest, ruff, Postgres (psycopg3), FastAPI, vanilla JS (dashboard).

**Spec:** `docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md`

## Global Constraints

- No env-var fallback for any DB-only value, old (cooldown trio, usage-cap pair, `REVIEW_DRAFT_PRS`) or new (9 tuning knobs, per-slot model/project/location). A missing value is a real failure, not a silent default.
- `Settings` fields for these values change *role* only: they're the one-time seed value `_seed_runtime_config_defaults`/`--sync-config-db` writes into the DB, never read at request/dispatch time again.
- Credentials (`GEMINI_API_KEY`, `GROQ_API_KEY`, `VERTEX_GCP_SERVICE_ACCOUNT_KEY`[+slots]) never move to the DB — untouched by this plan.
- `slot_config` rows are durable per `(provider, slot_index)`, independent of `runtime_config`'s `*_key_index` (which slot is *active*) — switching the active slot never reads or writes `slot_config`.
- **A consequence not spelled out verbatim in the spec, flagged here explicitly**: once `active_model()` has no env fallback, the flat `GEMINI_MODEL`/`GROQ_MODEL`/`VERTEX_MODEL` Render env vars become permanently unread — nothing in this plan's design ever falls back to them again. This plan removes all three from `render.yaml`/`.env.example`/`scripts/deploy.py`'s pushed vars too (Task 12), on top of the spec's explicitly-named 11. If that's not intended, stop after Task 12's `git diff` and check with the user before continuing — everything past that point assumes it.
- Every task: run its listed tests, then the full suite + ruff, before committing.

---

### Task 1: `slot_config` table + store.py CRUD

**Files:**
- Modify: `review_queue/store.py:40-56` (`RUNTIME_CONFIG_COLUMNS`, add 9 new tuning-knob columns), `review_queue/store.py:84-111` (`_SCHEMA`, add `slot_config` table)
- Test: `tests/test_store_slot_config.py` (new)

**Interfaces:**
- Produces: `store.get_slot_config(provider: str, slot_index: int) -> dict | None` (keys: `model`, `vertex_gcp_project`, `vertex_gcp_location`, all `str | None`), `store.set_slot_config(provider: str, slot_index: int, *, model: str | None, vertex_gcp_project: str | None, vertex_gcp_location: str | None, now: str) -> None` (upsert, all three fields written together — never a partial-field update), `store.delete_slot_config(provider: str, slot_index: int) -> None`, `store.get_all_slot_configs() -> dict[tuple[str, int], dict]` (for the dispatcher refresh — one query, not N).

- [ ] **Step 1: Add the table to `_SCHEMA` and the new tuning-knob columns to `RUNTIME_CONFIG_COLUMNS`**

In `review_queue/store.py`, add to `RUNTIME_CONFIG_COLUMNS` (after `("review_draft_prs", "BOOLEAN"),`):

```python
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
```

(`dispatcher_idle_sleep_seconds` lives in the same table as the other 8 — only its *refresh cadence* differs, per Task 6; the storage is identical.)

Add a new table to `_SCHEMA` (the module-level string built from `_SCHEMA_COLUMNS_SQL` — find the `CREATE TABLE IF NOT EXISTS runtime_config` block and add this immediately after its closing `ENABLE ROW LEVEL SECURITY;` line):

```sql
CREATE TABLE IF NOT EXISTS slot_config (
    provider            TEXT    NOT NULL,
    slot_index          INTEGER NOT NULL,
    model               TEXT,
    vertex_gcp_project  TEXT,
    vertex_gcp_location TEXT,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (provider, slot_index)
);
ALTER TABLE slot_config ENABLE ROW LEVEL SECURITY;
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_store_slot_config.py
"""review_queue/store.py's slot_config CRUD: durable per (provider, slot_index),
independent of which slot is active (runtime_config's *_key_index columns) --
see docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 4a."""
from __future__ import annotations

import pytest

from review_queue import store

pytestmark = pytest.mark.usefixtures("db")


def test_get_slot_config_returns_none_for_unconfigured_slot():
    assert store.get_slot_config("groq", 3) is None


def test_set_then_get_round_trips_all_three_fields():
    store.set_slot_config(
        "vertex", 1,
        model="gemini-2.5-flash",
        vertex_gcp_project="proj-a",
        vertex_gcp_location="us-east1",
        now="2026-09-08T00:00:00+00:00",
    )
    row = store.get_slot_config("vertex", 1)
    assert row == {
        "model": "gemini-2.5-flash",
        "vertex_gcp_project": "proj-a",
        "vertex_gcp_location": "us-east1",
    }


def test_gemini_groq_rows_leave_vertex_only_fields_null():
    store.set_slot_config(
        "groq", 0, model="llama-3.3-70b-versatile",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    row = store.get_slot_config("groq", 0)
    assert row["model"] == "llama-3.3-70b-versatile"
    assert row["vertex_gcp_project"] is None
    assert row["vertex_gcp_location"] is None


def test_switching_active_slot_never_touches_other_slots_config():
    """The persistence guarantee from spec section 4a: configuring slot 2,
    then setting slot 0 as active elsewhere (runtime_config, not touched by
    this module at all), must never affect slot 2's stored row."""
    store.set_slot_config(
        "gemini", 2, model="gemini-2.0-flash-exp",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    store.set_key_index_override("gemini", 0, now="2026-09-08T00:00:01+00:00")
    row = store.get_slot_config("gemini", 2)
    assert row["model"] == "gemini-2.0-flash-exp"


def test_delete_removes_the_row_entirely():
    store.set_slot_config(
        "groq", 1, model="gemma2-9b-it",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    store.delete_slot_config("groq", 1)
    assert store.get_slot_config("groq", 1) is None


def test_get_all_slot_configs_returns_every_configured_row_keyed_by_provider_and_slot():
    store.set_slot_config(
        "groq", 0, model="llama-3.3-70b-versatile",
        vertex_gcp_project=None, vertex_gcp_location=None,
        now="2026-09-08T00:00:00+00:00",
    )
    store.set_slot_config(
        "vertex", 1, model="gemini-2.5-flash",
        vertex_gcp_project="proj-a", vertex_gcp_location="us-east1",
        now="2026-09-08T00:00:00+00:00",
    )
    all_configs = store.get_all_slot_configs()
    assert all_configs[("groq", 0)]["model"] == "llama-3.3-70b-versatile"
    assert all_configs[("vertex", 1)]["vertex_gcp_project"] == "proj-a"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_store_slot_config.py -v`
Expected: FAIL — `AttributeError: module 'review_queue.store' has no attribute 'get_slot_config'` (and the table doesn't exist yet either, but the attribute error fires first).

- [ ] **Step 4: Implement the CRUD functions**

Add to `review_queue/store.py`, near the other `get_*_override`/`set_*_override` functions:

```python
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
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_store_slot_config.py -v`
Expected: PASS, all 6 tests.

- [ ] **Step 6: Commit**

```bash
git add review_queue/store.py tests/test_store_slot_config.py
git commit -m "Add slot_config table + CRUD, and the 9 new tuning-knob runtime_config columns"
```

---

### Task 2: Extend `_seed_runtime_config_defaults` for the 9 new tuning knobs

**Files:**
- Modify: `review_queue/store.py` (`_seed_runtime_config_defaults`)
- Test: `tests/test_store.py` or wherever `_seed_runtime_config_defaults`'s existing seed test lives — `grep -rn "_seed_runtime_config_defaults\|cooldown_base_seconds ==" tests/` to find it.

**Interfaces:**
- Consumes: `Settings.llm_request_timeout_seconds`, `.dispatcher_default_retry_after_seconds`, `.dispatcher_failure_base_backoff_seconds`, `.dispatcher_failure_max_backoff_seconds`, `.dispatcher_max_failure_attempts`, `.dispatcher_max_notice_post_attempts`, `.dispatcher_min_retry_after_seconds`, `.dispatcher_backoff_jitter_seconds`, `.dispatcher_notice_sweep_batch_size`, `.dispatcher_idle_sleep_seconds` (all already exist on `Settings` today, unchanged by this plan).

This is the actual "hardening" mechanism for these 9 vars — not new deploy-script logic. `_seed_runtime_config_defaults` already guarantees the 6 existing DB-only vars have a real row the instant the service first boots against a fresh database (`ON CONFLICT (id) DO NOTHING`, so it only ever fills a genuinely empty singleton row, never overwrites an operator's later change). Widening its one `INSERT` to include the 9 new columns gives them the exact same guarantee for free.

- [ ] **Step 1: Find the existing seed test**

```bash
grep -rln "_seed_runtime_config_defaults\|test.*seed.*runtime_config" tests/
```

Read whatever it asserts today (almost certainly: fresh `db` fixture, call whatever triggers `init_pool()`/`recover_on_startup`, assert the singleton row's cooldown/usage-cap/review-draft columns match `Settings` defaults). Add assertions for the 9 new columns following the exact same shape.

- [ ] **Step 2: Run to verify the new assertions fail**

Run: `uv run pytest <that test file> -v`
Expected: FAIL — the new columns read back `None`, not the `Settings` defaults, since the INSERT doesn't populate them yet.

- [ ] **Step 3: Widen the INSERT**

In `review_queue/store.py::_seed_runtime_config_defaults`, change the `INSERT INTO runtime_config (...)` to also list the 9 new columns and their `Settings` values, in the same order as `RUNTIME_CONFIG_COLUMNS` (Task 1, Step 1):

```python
    conn.execute(
        "INSERT INTO runtime_config ("
        "    id, updated_at, cooldown_base_seconds, cooldown_max_seconds,"
        "    cooldown_factor, key_usage_token_cap, key_usage_reset_time_utc,"
        "    review_draft_prs, llm_request_timeout_seconds,"
        "    dispatcher_default_retry_after_seconds,"
        "    dispatcher_failure_base_backoff_seconds,"
        "    dispatcher_failure_max_backoff_seconds, dispatcher_max_failure_attempts,"
        "    dispatcher_max_notice_post_attempts, dispatcher_min_retry_after_seconds,"
        "    dispatcher_backoff_jitter_seconds, dispatcher_notice_sweep_batch_size,"
        "    dispatcher_idle_sleep_seconds"
        ") VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            datetime.now(timezone.utc).isoformat(),
            settings.dispatcher_rereview_cooldown_seconds,
            settings.dispatcher_rereview_cooldown_max_seconds,
            settings.dispatcher_rereview_cooldown_factor,
            settings.key_usage_token_cap,
            settings.key_usage_reset_time_utc.isoformat(),
            settings.review_draft_prs,
            settings.llm_request_timeout_seconds,
            settings.dispatcher_default_retry_after_seconds,
            settings.dispatcher_failure_base_backoff_seconds,
            settings.dispatcher_failure_max_backoff_seconds,
            settings.dispatcher_max_failure_attempts,
            settings.dispatcher_max_notice_post_attempts,
            settings.dispatcher_min_retry_after_seconds,
            settings.dispatcher_backoff_jitter_seconds,
            settings.dispatcher_notice_sweep_batch_size,
            settings.dispatcher_idle_sleep_seconds,
        ),
    )
```

Also update the function's own docstring (it currently only names cooldown/usage-cap/review-draft) to mention the 9 new columns follow the identical contract.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest <that test file> -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add review_queue/store.py tests/
git commit -m "Seed the 9 new tuning-knob columns on first boot, same guarantee as the existing 6"
```

---

### Task 3: Per-tuning-knob cache modules + store getters

**Files:**
- Create: `review_queue/dispatcher_tuning_config.py`
- Modify: `review_queue/store.py` (add `get_dispatcher_tuning_config`/`set_dispatcher_tuning_config`, mirroring `get_cooldown_overrides`'s shape)
- Test: `tests/test_dispatcher_tuning_config.py` (new)

**Interfaces:**
- Produces: `review_queue.dispatcher_tuning_config.effective_config() -> dict` with keys `llm_request_timeout_seconds`, `dispatcher_default_retry_after_seconds`, `dispatcher_failure_base_backoff_seconds`, `dispatcher_failure_max_backoff_seconds`, `dispatcher_max_failure_attempts`, `dispatcher_max_notice_post_attempts`, `dispatcher_min_retry_after_seconds`, `dispatcher_backoff_jitter_seconds`, `dispatcher_notice_sweep_batch_size` — every value real (never `None`) once the DB is seeded (Task 2), so callers index this dict directly, no per-field fallback logic needed at call sites. `set_override_cache(config: dict) -> None`, `reset_override_cache() -> None`.
- `store.get_dispatcher_tuning_config() -> dict` (same 9 keys, values `None` only if the singleton row genuinely doesn't exist — i.e. Task 2's seed never ran, which should not happen in production but must not crash a test that doesn't use the `db` fixture's full boot sequence).

Deliberately **one shared cache module for all 8 non-idle-sleep knobs**, not 8 separate ones — they're always refreshed together (one query) and always consumed together conceptually (dispatcher tuning), unlike `cooldown_config`/`usage_cap_config`/`review_draft_config`'s separate modules (which predate this plan and aren't being merged — see Task 8, this is deliberately not proposing to also merge those).

- [ ] **Step 1: `store.py` getter**

```python
def get_dispatcher_tuning_config() -> dict:
    """The 9 tuning-knob values in force. All 9 keys always present; a value
    is None only if the singleton row itself doesn't exist yet (should never
    happen once _seed_runtime_config_defaults has run) -- see
    review_queue/dispatcher_tuning_config.py for the no-fallback policy this
    feeds."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT llm_request_timeout_seconds, dispatcher_default_retry_after_seconds,"
            "    dispatcher_failure_base_backoff_seconds, dispatcher_failure_max_backoff_seconds,"
            "    dispatcher_max_failure_attempts, dispatcher_max_notice_post_attempts,"
            "    dispatcher_min_retry_after_seconds, dispatcher_backoff_jitter_seconds,"
            "    dispatcher_notice_sweep_batch_size "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    keys = (
        "llm_request_timeout_seconds", "dispatcher_default_retry_after_seconds",
        "dispatcher_failure_base_backoff_seconds", "dispatcher_failure_max_backoff_seconds",
        "dispatcher_max_failure_attempts", "dispatcher_max_notice_post_attempts",
        "dispatcher_min_retry_after_seconds", "dispatcher_backoff_jitter_seconds",
        "dispatcher_notice_sweep_batch_size",
    )
    if row is None:
        return {k: None for k in keys}
    return {k: row[k] for k in keys}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_dispatcher_tuning_config.py
"""review_queue/dispatcher_tuning_config.py: no env fallback, per
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 10.4."""
from __future__ import annotations

from review_queue import dispatcher_tuning_config as tuning


def test_starts_empty_and_reads_back_none_before_any_refresh():
    tuning.reset_override_cache()
    assert tuning.effective_config() == {}


def test_set_override_cache_is_what_effective_config_returns():
    config = {
        "llm_request_timeout_seconds": 45.0,
        "dispatcher_default_retry_after_seconds": 60.0,
        "dispatcher_failure_base_backoff_seconds": 2.0,
        "dispatcher_failure_max_backoff_seconds": 300.0,
        "dispatcher_max_failure_attempts": 5,
        "dispatcher_max_notice_post_attempts": 3,
        "dispatcher_min_retry_after_seconds": 1.0,
        "dispatcher_backoff_jitter_seconds": 0.0,
        "dispatcher_notice_sweep_batch_size": 20,
    }
    tuning.set_override_cache(config)
    assert tuning.effective_config() == config
    tuning.reset_override_cache()
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_dispatcher_tuning_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'review_queue.dispatcher_tuning_config'`.

- [ ] **Step 4: Implement the module**

```python
# review_queue/dispatcher_tuning_config.py
"""The 9 dispatcher/timeout tuning knobs actually in force -- DB-only, no
env fallback. Mirrors review_queue/cooldown_config.py's cache-refresh shape
exactly, minus the fallback branch: see docs/superpowers/specs/2026-09-08-
slotted-config-and-db-delegation-design.md section 10.4 for why the
fallback was removed (the singleton row is guaranteed seeded by
store._seed_runtime_config_defaults on first boot, so "missing" no longer
needs a graceful degrade -- it would only ever mean a genuine setup bug,
which should be visible, not papered over).

Every read goes through effective_config(). The DB read lives in the
dispatcher (asyncio.to_thread convention); pushed in via set_override_cache,
keeping this module import-light and non-blocking.

An empty cache (before the first refresh, or after a failed one) reads back
as {} -- callers must treat that as "config not yet available this tick",
not synthesize a default for it. In practice the dispatcher's own refresh
call (review_queue/dispatcher.py::process_next_due) runs before any of
these values are consulted, so {} is only ever transiently observable in a
test that calls effective_config() without first calling set_override_cache.
"""

from __future__ import annotations

_config: dict = {}


def effective_config() -> dict:
    return dict(_config)


def set_override_cache(config: dict) -> None:
    global _config
    _config = config


def reset_override_cache() -> None:
    set_override_cache({})
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_dispatcher_tuning_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add review_queue/store.py review_queue/dispatcher_tuning_config.py tests/test_dispatcher_tuning_config.py
git commit -m "Add dispatcher_tuning_config cache module (8 of the 9 new tuning knobs)"
```

---

### Task 4: `DISPATCHER_IDLE_SLEEP_SECONDS` — throttled refresh, separate from the rest

**Files:**
- Modify: `review_queue/dispatcher_tuning_config.py` (or a small sibling — see below), `review_queue/dispatcher.py:532-559` (`run_forever`)
- Modify: `review_queue/store.py` (add `get_idle_sleep_seconds() -> float | None`, a one-column read — reusing the 9-key `get_dispatcher_tuning_config` for a value read on every idle tick would waste 8/9 of the query)
- Test: `tests/test_dispatcher.py` (extend — find the existing `run_forever`/idle-sleep test)

**Interfaces:**
- Produces: a throttled refresh — `run_forever()` gains a `_last_idle_sleep_refresh: float` (monotonic timestamp) module-level var and only calls `asyncio.to_thread(store.get_idle_sleep_seconds)` when more than a threshold (`_IDLE_SLEEP_REFRESH_INTERVAL_SECONDS = 30`, a new module constant — 30s means at most one extra DB query per 30s of idle polling, regardless of how short the idle-sleep value itself is) has elapsed since the last refresh.

This is the one exception the spec calls out (section 6): the per-claimed-ticket refresh never fires while the queue is idle, which is exactly when this value matters, so it needs its own site.

- [ ] **Step 1: `store.py` getter**

```python
def get_idle_sleep_seconds() -> float | None:
    """DISPATCHER_IDLE_SLEEP_SECONDS's DB value, or None if unset (should not
    happen once seeded -- see get_dispatcher_tuning_config's docstring)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT dispatcher_idle_sleep_seconds FROM runtime_config WHERE id = 1"
        ).fetchone()
    return None if row is None else row["dispatcher_idle_sleep_seconds"]
```

- [ ] **Step 2: Find and read the existing idle-sleep test**

```bash
grep -n "dispatcher_idle_sleep_seconds\|run_forever" tests/test_dispatcher.py
```

Read what it currently asserts (almost certainly: `run_forever` sleeps `settings.dispatcher_idle_sleep_seconds` between iterations, via a mocked `asyncio.sleep`).

- [ ] **Step 3: Write the new failing test**

```python
async def test_run_forever_refreshes_idle_sleep_from_db_not_settings(monkeypatch):
    """DISPATCHER_IDLE_SLEEP_SECONDS is DB-only (no env fallback) -- run_forever
    must sleep the DB value, not settings.dispatcher_idle_sleep_seconds, once
    a refresh has happened."""
    from review_queue import dispatcher, store

    monkeypatch.setattr(store, "get_idle_sleep_seconds", lambda: 7.5)
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError  # stop run_forever after one iteration

    monkeypatch.setattr(dispatcher.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(dispatcher, "process_next_due", AsyncMock(return_value=None))
    monkeypatch.setattr(dispatcher, "post_pending_notices", AsyncMock(return_value=0))
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.run_forever()
    assert sleeps == [7.5]
```

(Adjust imports/mocking helpers to match whatever this test file already uses for `run_forever` tests — read its existing `run_forever` test first and follow its exact mocking style rather than inventing a new one.)

- [ ] **Step 4: Run to verify failure**

Run: `uv run pytest tests/test_dispatcher.py -k idle_sleep -v`
Expected: FAIL — `run_forever` still sleeps `settings.dispatcher_idle_sleep_seconds` (a `Settings` default, not `7.5`).

- [ ] **Step 5: Implement the throttled refresh in `run_forever`**

In `review_queue/dispatcher.py`, add near the top-level module state (alongside `_blocked_until`):

```python
_IDLE_SLEEP_REFRESH_INTERVAL_SECONDS = 30
_idle_sleep_seconds: float | None = None
_last_idle_sleep_refresh: float = 0.0
```

Change `run_forever`'s final line from `await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)` to:

```python
    global _idle_sleep_seconds, _last_idle_sleep_refresh
    now_monotonic = time.monotonic()
    if now_monotonic - _last_idle_sleep_refresh >= _IDLE_SLEEP_REFRESH_INTERVAL_SECONDS:
        try:
            _idle_sleep_seconds = await asyncio.to_thread(store.get_idle_sleep_seconds)
        except Exception:  # noqa: BLE001
            logger.exception("failed to refresh idle-sleep-seconds; reusing last known value")
        _last_idle_sleep_refresh = now_monotonic
    await asyncio.sleep(_idle_sleep_seconds if _idle_sleep_seconds is not None else 1.0)
```

(`1.0` is a hardcoded final fallback for the narrow window before the *first* successful refresh ever completes — not an env fallback, just "don't busy-loop with `None`"; add `import time` at the top of the file if not already present.)

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_dispatcher.py -k idle_sleep -v`
Expected: PASS.

- [ ] **Step 7: Run the full dispatcher test file**

Run: `uv run pytest tests/test_dispatcher.py tests/test_dispatcher_backoff.py -v`
Expected: PASS (no regressions in the pre-existing `run_forever` tests — if one now fails because it asserted the old `settings.dispatcher_idle_sleep_seconds` sleep value directly, update it to seed `_idle_sleep_seconds`/mock `store.get_idle_sleep_seconds` instead, following this task's own new test as the pattern).

- [ ] **Step 8: Commit**

```bash
git add review_queue/store.py review_queue/dispatcher.py tests/test_dispatcher.py
git commit -m "Throttled DB refresh for DISPATCHER_IDLE_SLEEP_SECONDS in run_forever's main loop"
```

---

### Task 5: Wire the 8 shared tuning knobs into `dispatcher.py` + provider timeout/retry reads

**Files:**
- Modify: `review_queue/dispatcher.py` (`_jitter`, `compute_backoff`, and every other `settings.dispatcher_*`/`settings.llm_request_timeout_seconds` read except idle-sleep — re-run `grep -n "settings\.dispatcher_\|settings\.llm_request_timeout_seconds" review_queue/dispatcher.py providers/groq.py providers/google_genai.py` to get the exact current line numbers, since Task 4 already changed some line numbers in `dispatcher.py`), `providers/groq.py:61,69`, `providers/google_genai.py:38,73,116`
- Test: `tests/test_dispatcher_backoff.py`, `tests/test_provider_rate_limited.py`, `tests/test_providers.py`

**Interfaces:**
- Consumes: `review_queue.dispatcher_tuning_config.effective_config()` (Task 3).

- [ ] **Step 1: Add the refresh call to `process_next_due`**

In `review_queue/dispatcher.py::process_next_due`, alongside the existing `_refresh_model_overrides()`/`_refresh_usage_cap_overrides()`/`_refresh_review_draft_override()` calls, add:

```python
async def _refresh_dispatcher_tuning_config() -> None:
    """Refresh the 8 shared tuning knobs once per claimed ticket, same
    cadence and fail-safe shape as the refreshes above -- a DB-read failure
    must never abort a review, but (unlike the old cooldown/usage-cap
    pattern) there is no env default to degrade to: reset_override_cache()
    empties the cache, and effective_config() reading {} is a real
    missing-config state a caller must surface, not silently patch over."""
    try:
        config = await asyncio.to_thread(store.get_dispatcher_tuning_config)
        dispatcher_tuning_config.set_override_cache(config)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh dispatcher tuning config")
        dispatcher_tuning_config.reset_override_cache()
```

Call it from `process_next_due` alongside the other `await _refresh_*()` calls, and add `from review_queue import dispatcher_tuning_config` (or add it to the existing `from review_queue import cooldown_config, review_draft_config, store, usage_cap_config` import line) at the top of the file.

- [ ] **Step 2: Replace every `settings.dispatcher_*`/`settings.llm_request_timeout_seconds` read (except idle-sleep, done in Task 4) with `dispatcher_tuning_config.effective_config()[...]`**

`_jitter()`:
```python
def _jitter() -> float:
    jitter_max = dispatcher_tuning_config.effective_config()["dispatcher_backoff_jitter_seconds"]
    if jitter_max <= 0:
        return 0.0
    return random.uniform(0.0, jitter_max)
```

`compute_backoff()`:
```python
def compute_backoff(attempts: int, jitter: float = 0.0) -> float:
    config = dispatcher_tuning_config.effective_config()
    base = config["dispatcher_failure_base_backoff_seconds"]
    cap = config["dispatcher_failure_max_backoff_seconds"]
    return min(base * 2 ** (attempts - 1), cap) + jitter
```

Repeat this same substitution (`dispatcher_tuning_config.effective_config()["<key>"]` in place of `settings.<field>`) at every other call site the grep in this task's header found — each is a one-line change, same shape.

`providers/groq.py`/`providers/google_genai.py`: these read `settings.llm_request_timeout_seconds`/`settings.dispatcher_default_retry_after_seconds` directly. Import `from review_queue import dispatcher_tuning_config` there too and substitute the same way — this does mean `providers/` now depends on `review_queue/`, a new edge; if that's an unwanted layering violation (check `CLAUDE.md`'s module-boundary section for whether `providers/` is documented as never importing `review_queue/`), stop and flag it rather than introduce it silently — the alternative is threading the timeout/retry-default values into each provider's `.complete()` call as parameters instead, sourced from `dispatcher.py` (which already imports both) at the call site in `orchestrator.py`.

- [ ] **Step 3: Update the existing tests for the new no-fallback contract**

`tests/test_dispatcher_backoff.py`, `tests/test_provider_rate_limited.py`, `tests/test_providers.py` currently set up their fixtures via `monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", ...)` (or similar) — these must change to `dispatcher_tuning_config.set_override_cache({...})` with the full 9-key dict instead, since the code no longer reads `settings` for these values at all. Read each test's current setup and convert it one at a time — this is mechanical but touches several tests, don't batch-and-hope; run each file individually after converting it.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_dispatcher_backoff.py tests/test_provider_rate_limited.py tests/test_providers.py tests/test_dispatcher.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add review_queue/dispatcher.py providers/groq.py providers/google_genai.py tests/
git commit -m "Wire the 8 shared dispatcher tuning knobs to dispatcher_tuning_config, no env fallback"
```

---

### Task 6: Retrofit `cooldown_config.py` / `usage_cap_config.py` / `review_draft_config.py` — drop the env-fallback branch

**Files:**
- Modify: `review_queue/cooldown_config.py`, `review_queue/usage_cap_config.py`, `review_queue/review_draft_config.py`
- Test: `tests/test_cooldown_config.py`, `tests/test_usage_cap_config.py`, `tests/test_review_draft_config.py`

**Interfaces:**
- Produces: `cooldown_config.effective_config()` returns whatever's cached, `(None, None, None)` if nothing's been refreshed yet (was: fall back to `settings.dispatcher_rereview_cooldown_*`). Same shape change for `usage_cap_config.effective_caps()` (returns `(None, None)`) and `review_draft_config.effective_review_draft_prs()` (returns `None`, not `settings.review_draft_prs`).

This is safe because `_seed_runtime_config_defaults` (already shipped, unchanged) guarantees these columns are non-`NULL` from the moment the service first boots — removing the fallback doesn't introduce a new missing-value risk, it just deletes now-provably-dead code (the fallback branch could previously only ever fire in a narrow first-boot race, which the seed function's `ON CONFLICT DO NOTHING` already closes).

- [ ] **Step 1: `cooldown_config.py`** — remove the `if factor < 1.0 or base > cap or base <= 0 or cap <= 0: return (settings...)` fallback branch and the two `settings.dispatcher_rereview_cooldown_*` reads in `effective_config()`. New body:

```python
def effective_config() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) as cached -- None values mean "not yet refreshed
    this process", not "use a default": DB is the sole source of truth, per
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
    section 10.4. An override that reads back invalid (factor < 1, base >
    cap, non-positive base/cap) is discarded as a whole triple -- (None,
    None, None) -- same as an unrefreshed cache, so a caller can't tell "bad
    data" from "not refreshed yet" and must treat both the same way (defer
    the ticket, don't guess)."""
    if _base is None or _cap is None or _factor is None:
        return (None, None, None)
    if _factor < 1.0 or _base > _cap or _base <= 0 or _cap <= 0:
        return (None, None, None)
    return (_base, _cap, _factor)
```

Also delete the module's `from config import settings` import (no longer used) and update the module docstring (currently says "a DB override when set, else the env-configured defaults" — no longer true).

- [ ] **Step 2: `usage_cap_config.py`** — remove `_env_caps()` and both places it's called; `effective_caps()`'s new body:

```python
def effective_caps() -> tuple[int | None, time | None]:
    """(token cap, reset time) as cached. A cap of None (with a real reset
    time present) means the cap is intentionally disabled -- that's a valid
    configured state, not "unset"; a reset time of None means genuinely not
    yet refreshed/configured, since a cap being off never implies the reset
    time is meaningless (it still gates when a *future* cap would reset)."""
    if _reset is None:
        return (None, None)
    try:
        reset = time.fromisoformat(_reset)
    except ValueError:
        return (None, None)
    if _tokens is not None and _tokens <= 0:
        return (None, reset)
    return (_tokens, reset)
```

Delete the `from config import settings` import and update the docstring.

- [ ] **Step 3: `review_draft_config.py`** — `effective_review_draft_prs()`'s new body:

```python
def effective_review_draft_prs() -> bool | None:
    """The cached override, or None if never refreshed. Unlike the old
    behavior, None is NOT "treat drafts like non-drafts" -- callers must
    treat None as "config not available this tick" (defer/skip the
    decision), matching the no-fallback policy everywhere else in this
    plan."""
    return _override
```

Delete the `from config import settings` import and update the docstring.

- [ ] **Step 4: Update every call site that consumes these three functions**

```bash
grep -rn "cooldown_config.effective_config\|usage_cap_config.effective_caps\|review_draft_config.effective_review_draft_prs" *.py review_queue/*.py
```

Each call site currently assumes a real, always-usable tuple/bool. Read each one and add the "None means not-yet-available, treat like any other refresh failure this tick" handling — almost certainly this means: if the dispatcher's own `process_next_due` refresh for these ran and populated the cache moments earlier (same call), a `None` here can only mean the cache was reset due to a refresh failure or invalid data, which the surrounding code already has a defer/skip path for elsewhere (mirror that, don't invent a new one).

- [ ] **Step 5: Update the three modules' existing tests**

`tests/test_cooldown_config.py`, `tests/test_usage_cap_config.py`, `tests/test_review_draft_config.py` almost certainly have a test asserting "no override set → falls back to `settings.X`" — these are now wrong and must be replaced with "no override set → `effective_*()` returns the not-yet-refreshed shape (`None`s)". Read each file and convert its fallback-specific test(s); leave the override-round-trips-correctly tests as-is.

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_cooldown_config.py tests/test_usage_cap_config.py tests/test_review_draft_config.py tests/test_dispatcher.py tests/test_key_usage_cap.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add review_queue/cooldown_config.py review_queue/usage_cap_config.py review_queue/review_draft_config.py tests/
git commit -m "Retrofit cooldown/usage-cap/review-draft config: drop env fallback, DB sole source of truth"
```

---

### Task 7: Per-slot model resolution — widen `active_model.py`

**Files:**
- Modify: `providers/active_model.py`
- Test: `tests/test_active_model.py`

**Interfaces:**
- Produces: `active_model(provider: str, index: int) -> str | None` (was `active_model(provider: str) -> str`; **returns `None` on a missing value**, per spec section 10.5 — caller raises, this module stays dependency-free). `set_override_cache(configs: dict[tuple[str, int], str]) -> None` (was `dict[str, str]`).

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_active_model.py — read the existing file first and
# follow its exact style/fixture conventions; these replace whatever tests
# assert the old dict[str, str]-keyed / env-fallback behavior.
def test_active_model_is_keyed_by_provider_and_slot():
    from providers import active_model

    active_model.set_override_cache({("groq", 0): "llama-3.3-70b-versatile", ("groq", 1): "gemma2-9b-it"})
    assert active_model.active_model("groq", 0) == "llama-3.3-70b-versatile"
    assert active_model.active_model("groq", 1) == "gemma2-9b-it"
    active_model.reset_override_cache()


def test_active_model_returns_none_for_unconfigured_slot():
    from providers import active_model

    active_model.reset_override_cache()
    assert active_model.active_model("gemini", 0) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_active_model.py -v`
Expected: FAIL — `active_model()` still takes one positional arg.

- [ ] **Step 3: Rewrite the module**

```python
# providers/active_model.py
"""The model name actually in force per (provider, credential slot): a DB
override when set for that exact slot, else None. No env fallback -- see
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
sections 4b/10.5. A slot with no configured model is a real missing-config
state; the caller (providers/factory.py::_build) is responsible for turning
that into a visible failure. This module stays as dependency-free as
providers/key_index.py, on purpose -- it does no I/O and raises nothing
itself.
"""

from __future__ import annotations

_overrides: dict[tuple[str, int], str] = {}


def active_model(provider: str, index: int) -> str | None:
    """The model configured for this exact (provider, slot), or None if
    that slot has never been configured."""
    value = _overrides.get((provider, index))
    return value if value else None


def set_override_cache(overrides: dict[tuple[str, int], str]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_active_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add providers/active_model.py tests/test_active_model.py
git commit -m "Widen active_model() to (provider, slot)-keyed, no env fallback"
```

---

### Task 8: New `providers/active_vertex_slot.py` (project/location)

**Files:**
- Create: `providers/active_vertex_slot.py`
- Test: `tests/test_active_vertex_slot.py` (new)

**Interfaces:**
- Produces: `active_vertex_project(index: int) -> str | None`, `active_vertex_location(index: int) -> str | None`, `set_override_cache(configs: dict[int, tuple[str | None, str | None]]) -> None`, `reset_override_cache() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_active_vertex_slot.py
from __future__ import annotations

from providers import active_vertex_slot


def test_returns_none_for_unconfigured_slot():
    active_vertex_slot.reset_override_cache()
    assert active_vertex_slot.active_vertex_project(0) is None
    assert active_vertex_slot.active_vertex_location(0) is None


def test_returns_the_configured_slot_pair():
    active_vertex_slot.set_override_cache({1: ("proj-a", "us-east1")})
    assert active_vertex_slot.active_vertex_project(1) == "proj-a"
    assert active_vertex_slot.active_vertex_location(1) == "us-east1"
    active_vertex_slot.reset_override_cache()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_active_vertex_slot.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# providers/active_vertex_slot.py
"""Vertex's project/location actually in force per credential slot -- DB
override only, no env fallback. Mirrors providers/active_model.py exactly;
see that module's docstring and docs/superpowers/specs/2026-09-08-slotted-
config-and-db-delegation-design.md sections 4b/10.5."""

from __future__ import annotations

_overrides: dict[int, tuple[str | None, str | None]] = {}


def active_vertex_project(index: int) -> str | None:
    project, _ = _overrides.get(index, (None, None))
    return project if project else None


def active_vertex_location(index: int) -> str | None:
    _, location = _overrides.get(index, (None, None))
    return location if location else None


def set_override_cache(overrides: dict[int, tuple[str | None, str | None]]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_active_vertex_slot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add providers/active_vertex_slot.py tests/test_active_vertex_slot.py
git commit -m "Add active_vertex_slot.py (per-slot project/location, no env fallback)"
```

---

### Task 9: Dispatcher refresh for `slot_config` (model + vertex project/location together)

**Files:**
- Modify: `review_queue/dispatcher.py` (`process_next_due`)
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `store.get_all_slot_configs()` (Task 1), `providers.active_model.set_override_cache`/`reset_override_cache` (Task 7), `providers.active_vertex_slot.set_override_cache`/`reset_override_cache` (Task 8).

- [ ] **Step 1: Replace `_refresh_model_overrides` with a slot-aware version**

`_refresh_model_overrides` (today: `store.get_all_model_overrides()` → `dict[str, str]` → `active_model.set_override_cache`) is superseded — `get_all_model_overrides`/`set_model_override` (the flat per-provider model override) become dead code once slotting lands (nothing calls them after this task; a later cleanup task removes them, see Task 15's checklist). Replace the function body:

```python
async def _refresh_slot_config() -> None:
    """Refresh both per-slot model AND vertex project/location from the one
    slot_config table in a single query, same cadence and fail-safe shape as
    the other per-claimed-ticket refreshes. Supersedes the old flat
    _refresh_model_overrides -- see docs/superpowers/specs/2026-09-08-
    slotted-config-and-db-delegation-design.md section 4b."""
    try:
        all_configs = await asyncio.to_thread(store.get_all_slot_configs)
        model_overrides = {
            key: config["model"] for key, config in all_configs.items() if config["model"]
        }
        vertex_overrides = {
            slot: (config["vertex_gcp_project"], config["vertex_gcp_location"])
            for (provider, slot), config in all_configs.items()
            if provider == "vertex"
        }
        active_model.set_override_cache(model_overrides)
        active_vertex_slot.set_override_cache(vertex_overrides)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh slot_config; degrading to no configured slots")
        active_model.reset_override_cache()
        active_vertex_slot.reset_override_cache()
```

Update the call site in `process_next_due` (`await _refresh_model_overrides()` → `await _refresh_slot_config()`), and add `active_vertex_slot` to the `from providers import active, active_model, key_index` import line.

- [ ] **Step 2: Update `tests/test_dispatcher.py`'s existing model-override-refresh test(s)**

```bash
grep -n "_refresh_model_overrides\|get_all_model_overrides" tests/test_dispatcher.py
```

Read what's there and convert it to mock `store.get_all_slot_configs` (returning the new `dict[tuple[str,int], dict]` shape) instead of `store.get_all_model_overrides`, asserting both `active_model.set_override_cache` and `active_vertex_slot.set_override_cache` were called correctly. Also add a new test asserting a `get_all_slot_configs` failure resets *both* caches, mirroring the existing refresh-failure tests' shape for the other overrides.

- [ ] **Step 3: Run to verify pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add review_queue/dispatcher.py tests/test_dispatcher.py
git commit -m "Dispatcher refreshes slot_config (model + vertex project/location) together"
```

---

### Task 10: `providers/factory.py` — build from slot_config, raise on missing

**Files:**
- Modify: `providers/factory.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `active_model.active_model(provider, index)` (Task 7, now 2-arg), `active_vertex_slot.active_vertex_project(index)`/`active_vertex_location(index)` (Task 8).

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_providers.py -- read the existing factory tests first
# (test_factory_raises_for_unknown_provider et al.) and match their exact
# fixture/monkeypatch style.
def test_factory_raises_when_slot_has_no_configured_model(monkeypatch):
    from providers import active_model, credentials, factory

    monkeypatch.setattr(credentials, "resolve", lambda provider, index: ("GROQ_API_KEY", "real-key"))
    active_model.reset_override_cache()  # no model configured for any slot
    with pytest.raises(ValueError, match="no model configured"):
        factory._build("groq", 0, model=None)


def test_factory_raises_when_vertex_slot_has_no_project_or_location(monkeypatch):
    from providers import active_model, active_vertex_slot, factory, vertex_credentials

    active_model.set_override_cache({("vertex", 0): "gemini-2.5-flash"})
    monkeypatch.setattr(vertex_credentials, "resolve_service_account_info", lambda index: None)
    active_vertex_slot.reset_override_cache()  # no project configured, nothing to derive from either
    with pytest.raises(ValueError, match="no credential configured"):
        factory._build("vertex", 0, model="gemini-2.5-flash")
```

(`_build`'s signature is changing in this task — see Step 3 — so these tests may need adjusting once you see the real new signature; that's expected, this is the direction, not the literal final call shape if it turns out `model` shouldn't be a caller-supplied parameter anymore now that `_build` resolves it internally via `active_model`. Read `get_provider()`'s current caller code in Step 2 before finalizing these two tests.)

- [ ] **Step 2: Reconsider `get_provider()`'s model resolution**

Today, `get_provider()` calls `active_model(provider)` itself and passes `model` into `_build(provider, index, model)`. Since `active_model` is now 2-arg (`provider, index`) and can return `None`, `get_provider()` becomes:

```python
def get_provider() -> LLMProvider:
    provider = active_provider()
    index = key_index.active_key_index(provider)
    model = active_model.active_model(provider, index)
    if model is None:
        raise ValueError(
            f"no model configured for provider={provider!r} slot={index}"
        )
    cache_key = (provider, index, model)
    if cache_key not in _instances:
        _instances[cache_key] = _build(provider, index, model)
    return _instances[cache_key]
```

(the `None`-check moves *up* into `get_provider()`, ahead of the cache-key computation — a `None` model can't be part of a cache key that's later used to look up a real cached instance.) `_build` itself keeps taking `model: str` (never `None` by the time it's called) — so Task 1's first draft test above needs updating: call `get_provider()`, not `factory._build(..., model=None)` directly, to exercise the real raise site. Adjust accordingly once you're implementing this for real.

- [ ] **Step 3: Vertex branch — swap `settings.vertex_gcp_project`/`.vertex_gcp_location` for the slot-aware accessors**

In `_build`'s vertex branch:

```python
    if provider == "vertex":
        info = vertex_credentials.resolve_service_account_info(index)
        project = active_vertex_slot.active_vertex_project(index) or (info or {}).get("project_id", "")
        location = active_vertex_slot.active_vertex_location(index)
        if not project:
            raise ValueError(
                "no credential configured for provider='vertex': no project configured for "
                f"slot={index} and no service-account key found to derive it from"
            )
        if not location:
            raise ValueError(
                f"no location configured for provider='vertex' slot={index}"
            )
        from providers.google_genai import VertexProvider

        return VertexProvider(
            project=project,
            location=location,
            service_account_info=info,
            model=model,
        )
```

Add `from providers import active_vertex_slot` to the top of `providers/factory.py`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full provider/dispatcher test surface**

Run: `uv run pytest tests/test_providers.py tests/test_active_model.py tests/test_active_vertex_slot.py tests/test_dispatcher.py tests/test_provider_registry.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add providers/factory.py tests/test_providers.py
git commit -m "factory.py builds from slot_config via active_model/active_vertex_slot, raises when unconfigured"
```

---

### Task 11: Credential deletion nullifies its slot's `slot_config` row

**Files:**
- Modify: `config_deps.py` (`DeleteDependents`, `dependents_of`), `dashboard/environment.py` (`_cascade_delete` or wherever the confirmed-delete path lives — `grep -n "_cascade_delete\|has_dependents" dashboard/environment.py`), `dashboard/static/dashboard.html` (`env_delete_confirm_title`/`deleteConfirmList` rendering, if the label needs adding)
- Test: `tests/test_config_deps.py`, `dashboard/tests/test_environment.py`

**Interfaces:**
- Produces: `DeleteDependents.slot_config: bool` (new field), `.labels()` includes `"slotted model/project/location config"` when `True`, `.any()` includes it in the OR.
- Consumes: `store.get_slot_config(family, index)` (Task 1) from `dependents_of` — this makes `dependents_of` do I/O for the first time; check its current signature/callers (`dashboard/environment.py`) to see whether it's called from a sync or async context already reading other store state nearby, and pass the already-fetched row in as a parameter instead of having `dependents_of` reach into `store` itself if the existing function is meant to stay pure/I/O-free (its module docstring says "Pure logic, no I/O -- callers fetch the current runtime_config/Render state and pass it in" — **follow that existing convention**: add a `slot_config_row: dict | None` parameter to `dependents_of`, don't import `store` into `config_deps.py`).

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_config_deps.py
def test_dependents_of_flags_slot_config_for_an_inactive_spare_slot():
    """Independent of active/inactive -- a spare slot's leftover slot_config
    is exactly the ghost scenario this guards against."""
    dependents = dependents_of(
        "GROQ_API_KEY_3",
        key_index_overrides={"groq": 0},
        provider_override="groq",
        slot_config_row={"model": "gemma2-9b-it", "vertex_gcp_project": None, "vertex_gcp_location": None},
    )
    assert dependents.slot_config is True
    assert dependents.key_index_override is False  # slot 3 isn't the active one
    assert "slotted model/project/location config" in dependents.labels()


def test_dependents_of_does_not_flag_slot_config_when_none_configured():
    dependents = dependents_of(
        "GROQ_API_KEY_3", key_index_overrides={"groq": 0}, provider_override="groq",
        slot_config_row=None,
    )
    assert dependents.slot_config is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config_deps.py -v`
Expected: FAIL — `dependents_of() got an unexpected keyword argument 'slot_config_row'`.

- [ ] **Step 3: Implement**

```python
@dataclass
class DeleteDependents:
    key_index_override: bool = False
    provider_override: bool = False
    slot_config: bool = False

    def labels(self) -> list[str]:
        labels = []
        if self.key_index_override:
            labels.append("key_index override")
        if self.provider_override:
            labels.append("active provider override")
        if self.slot_config:
            labels.append("slotted model/project/location config")
        return labels

    def any(self) -> bool:
        return self.key_index_override or self.provider_override or self.slot_config


def dependents_of(
    var: str,
    *,
    key_index_overrides: dict[str, int],
    provider_override: str | None,
    slot_config_row: dict | None = None,
) -> DeleteDependents | None:
    for family in _SLOTTED_FAMILIES:
        index = slot_index_for_var(family, var)
        if index is None:
            continue
        active_slot = key_index_overrides.get(family, 0)
        is_active_slot = active_slot == index
        return DeleteDependents(
            key_index_override=key_index_overrides.get(family) == index,
            provider_override=(provider_override == family and is_active_slot),
            slot_config=slot_config_row is not None,
        )
    return None
```

Update the function's docstring — it currently says "deleting a credential slot never needs to touch a model var," which this task makes false.

- [ ] **Step 4: Wire the caller in `dashboard/environment.py`**

Find the call site (`grep -n "dependents_of(" dashboard/environment.py`), fetch `store.get_slot_config(family, index)` alongside whatever it already fetches (`key_index_overrides`, `provider_override`) before calling `dependents_of`, and pass it as `slot_config_row=`.

Then, in the actual delete-confirmed path (`_cascade_delete` or equivalent), when `dependents.slot_config` was true and the delete is confirmed, call `store.delete_slot_config(family, index)` alongside whatever else it already clears for `key_index_override`/`provider_override`.

- [ ] **Step 5: Add a dashboard-level test**

In `dashboard/tests/test_environment.py`, find the existing delete-confirm test(s) (`grep -n "deleteConfirmList\|has_dependents\|dependents" dashboard/tests/test_environment.py`) and add one asserting: deleting a credential slot with a configured `slot_config` row returns 409 with `"slotted model/project/location config"` in the dependents list, and confirming the delete actually removes the `slot_config` row (assert `store.get_slot_config(family, index) is None` afterward).

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_config_deps.py dashboard/tests/test_environment.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add config_deps.py dashboard/environment.py tests/test_config_deps.py dashboard/tests/test_environment.py
git commit -m "Deleting a credential slot deletes its slot_config row too, via the existing confirm-delete flow"
```

---

### Task 12: Guided-setup apply flow — split-push to Render (credential only) + `slot_config` (model/project/location)

**Files:**
- Modify: `dashboard/environment.py` (`_apply_llm_credential`, `ApplyLlmCredentialRequest`)
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Produces: `_apply_llm_credential` no longer pushes `model_var` to Render at all (per this plan's Global Constraints note) — only the credential var goes to Render. `model`, and for vertex `vertex_gcp_project`/`vertex_gcp_location`, go to `store.set_slot_config` as one call (all three fields together, per Task 1's "never a partial-field update" contract). `payload.clear_vertex_gcp_project` becomes a request to write `vertex_gcp_project=None` into that slot's `slot_config` row rather than deleting a Render env var (there is no more `VERTEX_GCP_PROJECT` Render env var to delete).

- [ ] **Step 1: Write the failing tests**

```python
# Add to dashboard/tests/test_environment.py -- read the existing
# _apply_llm_credential tests first (search for "credential/vertex/apply"
# or "credential/groq/apply") and match their exact request/mock style.
async def test_apply_no_longer_pushes_model_to_render(monkeypatch):
    """Model lives in slot_config only now -- Render never sees it."""
    pushed_keys = []
    monkeypatch.setattr(
        render_client, "push_env_var",
        lambda service_id, key, value: pushed_keys.append(key),
    )
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/groq/apply",
        json={"slot": 0, "credential": {"api_key": "real-key"}, "model": "llama-3.3-70b-versatile"},
    )
    assert resp.status_code == 200
    assert pushed_keys == ["GROQ_API_KEY"]  # not GROQ_MODEL


async def test_apply_writes_model_to_slot_config(monkeypatch):
    monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    client = await _client()
    await client.post(
        "/api/environment/credential/groq/apply",
        json={"slot": 2, "credential": {"api_key": "real-key"}, "model": "gemma2-9b-it"},
    )
    row = store.get_slot_config("groq", 2)
    assert row["model"] == "gemma2-9b-it"


async def test_apply_writes_vertex_project_and_location_to_slot_config(monkeypatch):
    monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    client = await _client()
    await client.post(
        "/api/environment/credential/vertex/apply",
        json={
            "slot": 0,
            "credential": {"service_account_b64": "..."},
            "model": "gemini-2.5-flash",
            "vertex_gcp_project": "proj-a",
            "vertex_gcp_location": "us-east1",
        },
    )
    row = store.get_slot_config("vertex", 0)
    assert row == {
        "model": "gemini-2.5-flash",
        "vertex_gcp_project": "proj-a",
        "vertex_gcp_location": "us-east1",
    }


async def test_apply_reports_slot_config_write_failure_as_a_named_failed_entry(monkeypatch):
    """Acceptance criterion from docs/superpowers/specs/2026-09-08-slotted-
    config-and-db-delegation-design.md section 8: a partial failure must be
    visible, not silently swallowed."""
    monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _boom(*a, **kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "set_slot_config", _boom)
    client = await _client()
    resp = await client.post(
        "/api/environment/credential/groq/apply",
        json={"slot": 0, "credential": {"api_key": "real-key"}, "model": "llama-3.3-70b-versatile"},
    )
    body = resp.json()
    assert "GROQ_API_KEY" in body["applied"]
    assert any(f["key"] == "slot_config.groq.0" for f in body["failed"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest dashboard/tests/test_environment.py -k apply -v`
Expected: FAIL — current code still pushes `model_var` to Render and calls `store.set_model_override`, not `store.set_slot_config`.

- [ ] **Step 3: Rewrite `_apply_llm_credential`**

```python
def _apply_llm_credential(family: str, payload: ApplyLlmCredentialRequest) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}

    credential_var = slot_env_name(family, payload.slot)
    credential_value = (
        payload.credential.get("service_account_b64", "")
        if family == "vertex"
        else payload.credential.get("api_key", "")
    )
    applied: list[str] = []
    failed: list[dict] = []
    try:
        render_client.push_env_var(service_id, credential_var, credential_value)
        applied.append(credential_var)
    except Exception as exc:  # noqa: BLE001
        failed.append({"key": credential_var, "error": type(exc).__name__})

    # Model (and for vertex, project/location) is DB-only now -- one upsert,
    # all fields together, never partial (store.set_slot_config's contract).
    slot_config_key = f"slot_config.{family}.{payload.slot}"
    try:
        vertex_gcp_project = (
            None if getattr(payload, "clear_vertex_gcp_project", False)
            else getattr(payload, "vertex_gcp_project", None)
        )
        store.set_slot_config(
            family, payload.slot,
            model=payload.model,
            vertex_gcp_project=vertex_gcp_project if family == "vertex" else None,
            vertex_gcp_location=getattr(payload, "vertex_gcp_location", None) if family == "vertex" else None,
            now=datetime.now(timezone.utc).isoformat(),
        )
        applied.append(slot_config_key)
    except Exception as exc:  # noqa: BLE001
        failed.append({"key": slot_config_key, "error": type(exc).__name__})

    if credential_var in applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}
```

Add `vertex_gcp_project: str | None = None` and `vertex_gcp_location: str | None = None` fields to `ApplyLlmCredentialRequest` (alongside the existing `clear_vertex_gcp_project: bool`), and import `store` at the top of `dashboard/environment.py` if not already imported there (it likely already is, for other endpoints — check first).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "Guided-setup apply: model/project/location go to slot_config only, credential stays on Render"
```

---

### Task 13: `scripts/deploy.py` — drop 14 vars from Render, add slot-0 seeding

**Files:**
- Modify: `scripts/deploy.py` (`_GENERIC_OPERATIONAL_ENV_ATTRS`, `_DB_SYNCED_OPERATIONAL_KEYS`, `_wanted_env`, `sync_env`)
- Test: `tests/test_deploy_script.py`

**Interfaces:** none new — this task only removes/relocates existing dict entries and adds one new sync step.

- [ ] **Step 1: Move the 11 spec-named vars from `_GENERIC_OPERATIONAL_ENV_ATTRS` to `_DB_SYNCED_OPERATIONAL_KEYS`**

`VERTEX_GCP_PROJECT`, `VERTEX_GCP_LOCATION`, `LLM_REQUEST_TIMEOUT_SECONDS`, `DISPATCHER_IDLE_SLEEP_SECONDS`, `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`, `DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS`, `DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS`, `DISPATCHER_MAX_FAILURE_ATTEMPTS`, `DISPATCHER_MAX_NOTICE_POST_ATTEMPTS`, `DISPATCHER_MIN_RETRY_AFTER_SECONDS`, `DISPATCHER_BACKOFF_JITTER_SECONDS`, `DISPATCHER_NOTICE_SWEEP_BATCH_SIZE` — delete all of these from `_GENERIC_OPERATIONAL_ENV_ATTRS` (`_wanted_env` stops pushing them to Render entirely), add them to `_DB_SYNCED_OPERATIONAL_KEYS`.

**Also remove `_PROVIDERS`' model-var entries from what's pushed** (this plan's Global Constraints note — `GEMINI_MODEL`/`GROQ_MODEL`/`VERTEX_MODEL` stop being Render vars too): find where `_wanted_env()` does `wanted[model_var] = getattr(settings, model_var.lower(), "")` for every provider and remove that block — model no longer has a Render presence at all once `active_model()` is DB-only (Task 7).

- [ ] **Step 2: `sync_env()` gains a `slot_config` seeding step for slot 0**

Right after `_wanted_env()`'s existing push loop (wherever `sync_env()` currently does the per-key `render_client.push_env_var` calls), add: for the currently-selected `settings.llm_provider`, if slot 0 has no `store.get_slot_config(provider, 0)` row yet, seed one from `Settings` (`settings.gemini_model`/`.groq_model`/`.vertex_model`, and for vertex `.vertex_gcp_project`/`.vertex_gcp_location`) — this is the slot_config equivalent of `_seed_runtime_config_defaults`, but scoped to CLI-driven initial setup rather than first-boot, since slot_config has no generic per-slot default the way the singleton `runtime_config` row does (see spec section 5 — a slot's config only makes sense once a credential exists there, and `sync_env()` is exactly the moment slot 0's credential is first being pushed).

```python
def _seed_slot_zero_config_if_missing() -> None:
    """The slot_config equivalent of store._seed_runtime_config_defaults,
    scoped to sync_env()'s slot-0 setup rather than first-boot -- slot_config
    has no universal default (only whichever slots actually have credentials
    need rows), so this seeds the currently-active provider's slot 0 the
    first time --sync-env runs, mirroring what Settings already has for it."""
    from review_queue import store

    provider = settings.llm_provider
    if provider not in _PROVIDERS:
        return
    if store.get_slot_config(provider, 0) is not None:
        return
    model_var = _PROVIDERS[provider][1]
    model = getattr(settings, model_var.lower(), None)
    vertex_gcp_project = settings.vertex_gcp_project if provider == "vertex" else None
    vertex_gcp_location = settings.vertex_gcp_location if provider == "vertex" else None
    store.set_slot_config(
        provider, 0,
        model=model, vertex_gcp_project=vertex_gcp_project, vertex_gcp_location=vertex_gcp_location,
        now=datetime.now(timezone.utc).isoformat(),
    )
    print(f"seeded slot_config for {provider} slot 0 (model={model!r})")
```

Call this from `sync_env()` right after the existing `--sync-config-db` call it already makes (or wherever the equivalent DB-sync step already happens in that function — read `sync_env()`'s current body to find the right spot; it must run only when `settings.database_url` is set, same guard the existing DB-sync work already uses).

- [ ] **Step 2: Update `tests/test_deploy_script.py`**

- The existing `test_render_yaml_declares_every_synced_var`-style test (built from `deploy._ALWAYS_SYNCED | {"LLM_PROVIDER"} | set(deploy._GENERIC_OPERATIONAL_ENV_ATTRS) | {credential, model_var for each provider}`) needs its expected set updated: model vars are no longer expected in `render.yaml` at all (remove `names.add(model_var)` from that test's own set-building, or however it's structured — read it first).
- Add a new test: `_wanted_env()`'s returned dict never contains any of the 14 removed keys.
- Add a new test: `sync_env()` (with a real `db` fixture) seeds `slot_config` for the active provider's slot 0 when none exists, and is a no-op when one already does (mirroring `_seed_runtime_config_defaults`'s own `ON CONFLICT DO NOTHING` idempotency, but at the Python level here since `set_slot_config` always upserts — so this function's own guard, the `if store.get_slot_config(...) is not None: return` early-return, is what makes it idempotent, not the SQL).

- [ ] **Step 3: Run to verify pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "deploy.py: 14 vars (11 tuning knobs + 3 model vars) off Render, seed slot_config for slot 0"
```

---

### Task 14: `render.yaml` + `.env.example` cleanup

**Files:**
- Modify: `render.yaml`, `.env.example`
- Test: `tests/test_deploy_script.py` (Task 13's updated `test_render_yaml_declares_every_synced_var` is the actual verification here)

- [ ] **Step 1: Remove the 14 vars' entries from `render.yaml`'s `envVars` list**

`VERTEX_GCP_PROJECT`, `VERTEX_GCP_LOCATION`, `LLM_REQUEST_TIMEOUT_SECONDS`, `DISPATCHER_IDLE_SLEEP_SECONDS`, `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`, `DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS`, `DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS`, `DISPATCHER_MAX_FAILURE_ATTEMPTS`, `DISPATCHER_MAX_NOTICE_POST_ATTEMPTS`, `DISPATCHER_MIN_RETRY_AFTER_SECONDS`, `DISPATCHER_BACKOFF_JITTER_SECONDS`, `DISPATCHER_NOTICE_SWEEP_BATCH_SIZE`, `GEMINI_MODEL`, `GROQ_MODEL`, `VERTEX_MODEL` — remove each `- key: <NAME>` block (matching how the existing 6 DB-only vars already have none there).

- [ ] **Step 2: Remove the same 14 from `.env.example`**

- [ ] **Step 3: Verify with `yaml.safe_load` + the deploy-script test**

```bash
python3 -c "import yaml; yaml.safe_load(open('render.yaml'))" && echo YAML_OK
uv run pytest tests/test_deploy_script.py -v
```

Expected: `YAML_OK`, tests PASS.

- [ ] **Step 4: Commit**

```bash
git add render.yaml .env.example
git commit -m "Remove the 14 DB-only vars from render.yaml/.env.example"
```

---

### Task 15: Dashboard — 9 new tuning-knob fields in the config panel

**Files:**
- Modify: `dashboard/environment.py` (`_build_config_payload`, `EnvironmentConfigPatch`, `_apply_config_patch`), `dashboard/static/dashboard.html` (`#configForm`, `ENV_VAR_DESCRIPTIONS`/`CONFIG_FIELD_ENV_VAR`-equivalent mapping, `populateConfigForm`, `saveConfig`)
- Test: `dashboard/tests/test_environment.py`, `dashboard/tests/test_dashboard_page.py`

**Interfaces:**
- Produces: `GET /api/environment/config` response gains the 9 new keys (same flat shape as the existing 7); `PATCH /api/environment/config` accepts them the same way.

This mirrors the exact pattern the 2026-09-08 sort/prettify session already built for the existing 7 config-panel fields (info-hover via a description map, alphabetical sort, `applied`/`failed` reporting) — read `dashboard/environment.py`'s `_build_config_payload`/`_apply_config_patch` and `dashboard/static/dashboard.html`'s `#configForm` block before starting, and add these 9 the same way, alphabetized alongside the existing 7 (16 fields total, sorted together — not two separate alphabetized groups).

- [ ] **Step 1: `dashboard/environment.py`** — extend `_build_config_payload` to read `store.get_dispatcher_tuning_config()` (Task 3) and merge its 9 keys into the response; extend `EnvironmentConfigPatch`/`_apply_config_patch` to accept and write them via a new `store.set_dispatcher_tuning_config(**fields, now=...)` (write-side counterpart to Task 3's getter — add this to `store.py` now if it doesn't exist yet, a plain `UPDATE runtime_config SET ... WHERE id = 1` mirroring `set_cooldown_override`'s shape).

- [ ] **Step 2: `dashboard.html`** — add 9 `<label>`+`<input>` pairs to `#configForm` with info-hover spans (mirroring the existing 7's exact markup shape from the 2026-09-08 work), a description mapping entry each (reuse wording from `ENV_VAR_DESCRIPTIONS`'s existing entries for these same var names where one already exists — e.g. `DISPATCHER_BACKOFF_JITTER_SECONDS` already has a description there from the earlier localization work), and extend `populateConfigForm`/`saveConfig` to read/write the 9 new `#cfg*` inputs. Re-sort the full 16-field list alphabetically by field key (matching the existing sort convention), keeping `provider` pinned above `#providerModelRows` as before.

- [ ] **Step 3: Tests**

Add assertions to `dashboard/tests/test_environment.py` for the new payload keys round-tripping through GET/PATCH, following the exact shape of the existing 7 fields' tests. Add a `dashboard/tests/test_dashboard_page.py` assertion that all 16 field ids appear in the served page.

- [ ] **Step 4: Run + `ui-visual-review`**

Run: `uv run pytest dashboard/tests/ -v`
Then invoke the `ui-visual-review` skill (per this project's own `CLAUDE.md` requirement for any `dashboard/static/` change) before considering this task done — screenshot light/dark/mobile, confirm the 16-field form still lays out correctly (no grid-blowout, matching the 2026-09-08 session's own verification approach).

- [ ] **Step 5: Commit**

```bash
git add dashboard/environment.py dashboard/static/dashboard.html dashboard/tests/
git commit -m "Add the 9 tuning-knob fields to the config panel (info-hover, sorted with the existing 7)"
```

---

### Task 16: Dashboard — per-slot editing UI (best-effort, per spec section 10.3)

**Files:**
- Modify: `dashboard/environment.py` (new endpoint or extend `_build_config_payload` to include `store.get_all_slot_configs()`), `dashboard/static/dashboard.html` (new UI section)
- Test: `dashboard/tests/test_environment.py`, `dashboard/tests/test_dashboard_page.py`

This is the one task in this plan without a fully pre-specified UI shape — per spec section 6/10.3, it's explicitly staged as best-effort in this same pass, closer in kind to the guided-setup modal's per-credential fields than the flat config rows. A reasonable minimum: a read-only list (per provider, every configured `(slot_index, model, project, location)` row from `store.get_all_slot_configs()`) rendered below the existing `#providerModelRows` grid, since editing already exists via guided-setup apply (Task 12) — a *direct* edit affordance (change a spare slot's model without re-running guided-setup) is a genuine nice-to-have, not required for this plan's core correctness goal, and can be deferred past this task if it doesn't fit the same pass cleanly.

- [ ] **Step 1: Expose the data** — extend `GET /api/environment/config`'s payload with a `slot_configs` key: `{"gemini": [{"slot": 0, "model": "..."}], "vertex": [{"slot": 0, "model": "...", "vertex_gcp_project": "...", "vertex_gcp_location": "..."}], ...}`, sourced from `store.get_all_slot_configs()` grouped by provider.

- [ ] **Step 2: Render it** — in `dashboard.html`, below `#providerModelRows`, render one line per configured slot showing its provider/slot/model (and project/location for vertex) — read-only for this task; a follow-up can add direct editing later if wanted.

- [ ] **Step 3: Test + `ui-visual-review`** — same as Task 15's Step 3/4.

- [ ] **Step 4: Commit**

```bash
git add dashboard/environment.py dashboard/static/dashboard.html dashboard/tests/
git commit -m "Show configured slots (model/project/location) in the config panel, read-only"
```

---

### Task 17: `onboarding-wizard` — provisioning seeds `slot_config`, drops model from Render push

**Files:** (in `~/onboarding-wizard`, a separate repo)
- Modify: `router.py`
- Test: `tests/test_onboarding_router.py`

**Interfaces:** none new outward-facing — internal to the wizard's own provisioning flow.

- [ ] **Step 1: Find the provisioning call site**

```bash
cd ~/onboarding-wizard
grep -n "_LLM_ENV_VAR_NAMES\|_GENERIC_OPERATIONAL_ENV_DEFAULTS" router.py
```

Read the function that actually pushes these to a newly-created Render service (likely `push_env_vars`/`create_service` per the earlier research) end to end.

- [ ] **Step 2: Stop pushing the model var; also drop the 11 tuning knobs from what's pushed as Render env vars**

`_LLM_ENV_VAR_NAMES`'s second tuple element (`GEMINI_MODEL`/`GROQ_MODEL`/`VERTEX_MODEL`) stops being pushed to Render — matching pr-review-bot's own `scripts/deploy.py` change (Task 13). Same for the 11 vars currently in `_GENERIC_OPERATIONAL_ENV_DEFAULTS` this plan moves off Render (all of them except `GITHUB_TARGET_REPO`/`LLM_REQUEST_TIMEOUT_SECONDS`'s... wait — re-derive the exact removal list from this plan's own Task 13 rather than re-deriving it independently: remove `VERTEX_GCP_PROJECT`, `VERTEX_GCP_LOCATION`, `LLM_REQUEST_TIMEOUT_SECONDS`, `DISPATCHER_IDLE_SLEEP_SECONDS`, `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`, `DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS`, `DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS`, `DISPATCHER_MAX_FAILURE_ATTEMPTS`, `DISPATCHER_MAX_NOTICE_POST_ATTEMPTS`, `DISPATCHER_MIN_RETRY_AFTER_SECONDS`, `DISPATCHER_BACKOFF_JITTER_SECONDS`, `DISPATCHER_NOTICE_SWEEP_BATCH_SIZE` from `_GENERIC_OPERATIONAL_ENV_DEFAULTS`.

- [ ] **Step 3: Add the DB-seeding step**

This wizard provisions the *database* too (Supabase, per its own provisioning flow) — find wherever it already writes to the new instance's Postgres (if it does; if the wizard only provisions Render and a separate Supabase step happens elsewhere, find that instead) and add: after the new instance's schema exists (`review_queue.store`'s `CREATE TABLE IF NOT EXISTS` runs on the new bot's own first boot, per pr-review-bot's existing `init_pool()`/`recover_on_startup` — check whether the wizard ever connects to the new instance's DB directly, or whether it must instead rely on the newly-deployed bot's own first-boot seeding (`_seed_runtime_config_defaults`, Task 2) to handle the 9 tuning knobs, and a **new equivalent step for slot 0's `slot_config`** the wizard pushes directly if it has DB access, or hands off to the bot's own first-boot logic if it doesn't. This is genuinely an open implementation question specific to this repo's architecture — read `onboarding-wizard/CLAUDE.md` and its Supabase-provisioning code before deciding which path it already supports, rather than guessing here.

- [ ] **Step 4: Update tests**

Convert whatever `tests/test_onboarding_router.py` assertions currently check "model var pushed" / "the 11 tuning knobs pushed" to assert they're *not* pushed, following this plan's Task 13-equivalent test changes as the pattern.

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/test_onboarding_router.py -v
uv run ruff check .
git add router.py tests/test_onboarding_router.py
git commit -m "Stop pushing model/tuning-knob vars to Render; seed slot_config where the wizard has DB access"
```

---

### Task 18: Final verification sweep (both repos)

- [ ] **Step 1: pr-review-bot — full suite + ruff + deploy-verify**

```bash
cd ~/pr-review-bot
uv run ruff check .
uv run pytest -q
bash .claude/skills/deploy-verify/verify_deploy_image.sh
```

Expected: ruff clean, full suite green, image builds and boots.

- [ ] **Step 2: Repo-wide grep for anything still reading the old flat model/tuning-knob settings at runtime**

```bash
grep -rn "settings\.gemini_model\|settings\.groq_model\|settings\.vertex_model\b" --include=*.py . | grep -v "^tests/\|^dashboard/tests/"
grep -rn "settings\.dispatcher_\(default_retry_after\|failure_base_backoff\|failure_max_backoff\|max_failure_attempts\|max_notice_post_attempts\|min_retry_after\|backoff_jitter\|notice_sweep_batch_size\|idle_sleep\)_seconds\b\|settings\.llm_request_timeout_seconds\b" --include=*.py . | grep -v "^tests/\|_seed_runtime_config_defaults\|sync_env\|_wanted_env"
```

Expected: no output outside `config.py`'s own field declarations and the seeding/CLI-sync call sites this plan deliberately keeps reading `Settings` for (Tasks 2, 13).

- [ ] **Step 3: onboarding-wizard — full suite + ruff**

```bash
cd ~/onboarding-wizard
uv run ruff check .
uv run pytest -q
```

Expected: both clean.

- [ ] **Step 4: Report back**

Summarize what changed, any deviations from this plan encountered during implementation (especially Task 5's providers→review_queue layering question and Task 17's DB-access question — both explicitly flagged above as needing a real decision during implementation, not this plan guessing blind), and current commit state (all local, nothing pushed, per this session's standing "defer pushing" instruction — confirm that's still what's wanted before pushing).

## Self-Review Notes

- **Spec coverage:** §4a/persistence → Task 1 (schema + tests proving switching slots doesn't touch other rows). §4a/scope → Task 1 (generic `provider` column, no per-provider table). §4b/no-fallback → Tasks 6, 7, 8, 9, 10. §5/seeding → Task 2 (flat knobs, reuses existing seed mechanism) + Task 13 (slot_config, new CLI-driven seed since no generic default exists). §6/migration table → Tasks 3, 4, 5, 13, 14. §7/dashboard → Tasks 15, 16. §8/split-push hardening → Task 12 (including its explicit partial-failure test cases). §9/credential-deletion cleanup → Task 11. §10 resolutions → threaded through as each resolved item's own task rather than restated separately.
- **Placeholder scan:** every step has real code or an exact command; the two spots without a fully pre-decided answer (Task 5's providers/review_queue layering, Task 17's DB-access path) are flagged as genuine implementation-time decisions with a clear default direction given, not vague "handle appropriately" language — consistent with how this plan's own predecessor (the env-var rename plan) hit and resolved unforeseen gaps during execution rather than pretending perfect foresight.
- **Type consistency:** `active_model(provider, index) -> str | None` (Task 7) is the exact signature Task 10's `get_provider()` calls; `store.get_slot_config`/`set_slot_config`/`get_all_slot_configs` (Task 1) are the exact names Tasks 9, 11, 12, 13, 15, 16 all consume; `dispatcher_tuning_config.effective_config()`'s 9-key dict shape (Task 3) is what Task 5's every substitution site indexes into.
