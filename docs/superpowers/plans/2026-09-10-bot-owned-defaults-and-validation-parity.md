# Bot-Owned Defaults and Validation Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pr-review-bot` self-heal its own `runtime_config`/`slot_config` shape and fill its own operational defaults at boot, and give the cooldown and usage-cap field groups the same shared-validator treatment the 9 dispatcher tuning knobs already have across every writer.

**Architecture:** `store.init_pool()` gains two idempotent, declarative steps after its existing `CREATE TABLE IF NOT EXISTS`: `ADD COLUMN IF NOT EXISTS` for any declared column the live table lacks, then a per-column `COALESCE` upsert that fills NULLs from `Settings`' declared class defaults without ever overwriting a value someone else wrote. In parallel, `cooldown_config` and `usage_cap_config` each gain a `problems(config) -> list[str]` function mirroring `dispatcher_tuning_config.problems()`, and all four writers (the CLI, the dashboard PATCH, the boot gate, the read path) are routed through them so none can disagree about what a usable value is.

**Tech Stack:** Python 3.12, psycopg3 + psycopg_pool, pydantic / pydantic-settings, FastAPI, pytest (+ pytest-xdist, `-n 4`), ruff, PostgreSQL 16.

**Spec:** `docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md` — this plan implements **Stage 1 only** (that spec's §10 rollout step 1: §3 backfill + ALTER, §4.3 shared predicates and the extended boot gate, and the corrected `store.py` docstrings). Stages 2–5, the cross-repo contract artifact, are a separate plan. Read the spec's §3 and §4 before starting; every task below argues from them.

## Global Constraints

- **Never log, print, or otherwise surface a secret value.** The backfill logs column *names* only, never values. `DATABASE_URL` never appears in an error message (`store.py`'s existing `_FIRST_CONNECT_HELP` convention). See `CLAUDE.md`'s "Secret handling" section, which overrides everything here.
- **Read `Settings`' declared class defaults, never the module-level `settings` instance,** for anything written into the database as a default. `Settings.model_fields[name].default` is environment-independent; `settings.<field>` picks up this machine's `.env`/`.env.config` and would let a developer's local config silently change what a production boot writes. Mirrors `scripts/gen_docs.py`'s "THE ONE RULE".
- **Declared, not migrated** (`store.py:65-72`). `ADD COLUMN IF NOT EXISTS` is permitted as a narrow, deliberate extension of the existing `ENABLE ROW LEVEL SECURITY` carve-out: idempotent, declarative, never a column-shape *change*. No `ALTER COLUMN`, no `DROP COLUMN`, no type changes, ever.
- **A column is ALTER-able only if it is nullable OR carries a `DEFAULT`.** `ADD COLUMN ... NOT NULL` with no default fails against a non-empty table.
- **Before pushing:** `uv run pytest -v` and `uv run ruff check .` must both be clean. Per `CLAUDE.md`, never push with a red suite or an unresolved lint error, and never skip either because a change looks too small.
- **Do not commit unless a task's step says to.** Never `git push` — this plan ends with commits on a branch, nothing more.
- Fast iteration subset while working: `uv run pytest -m "not db" -n 4`. Full suite before each commit.

---

### Task 1: The column-to-setting mapping and declared defaults

`runtime_config`'s column names are not `Settings`' field names for three of them, and one column's logical type differs from its wire type. Both facts are currently implicit inside `scripts/deploy.py::sync_config_db()`. This task extracts them into one module so the backfill (Task 6) and the CLI (Task 8) cannot disagree.

**Files:**
- Create: `review_queue/runtime_config_defaults.py`
- Test: `tests/test_runtime_config_defaults.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `COLUMN_TO_SETTING: dict[str, str]` — every `runtime_config` column that mirrors a `Settings` field, mapped to that field's name.
  - `declared_defaults() -> dict[str, float | int | bool | str]` — column name to its declared default, **excluding** any column whose declared default is `None`.
  - `NO_DEFAULT_BY_DESIGN: tuple[str, ...]` — columns deliberately left NULL.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runtime_config_defaults.py
"""COLUMN_TO_SETTING is the one place runtime_config's column names are
tied to Settings' field names -- they differ for the cooldown trio, and
key_usage_reset_time_utc's declared default is a `time` while its column
is TEXT. Both facts used to live only inside deploy.py::sync_config_db()."""
from datetime import time

from config import Settings
from review_queue import runtime_config_defaults as rcd
from review_queue.store import RUNTIME_CONFIG_COLUMNS


def test_every_mapped_column_exists_in_the_schema():
    declared = {name for name, _sql_type in RUNTIME_CONFIG_COLUMNS}
    unknown = set(rcd.COLUMN_TO_SETTING) - declared
    assert not unknown, f"COLUMN_TO_SETTING names no such column: {sorted(unknown)}"


def test_every_mapped_setting_exists_on_settings():
    unknown = set(rcd.COLUMN_TO_SETTING.values()) - set(Settings.model_fields)
    assert not unknown, f"COLUMN_TO_SETTING names no such Settings field: {sorted(unknown)}"


def test_cooldown_columns_map_to_their_differently_named_settings():
    # The rename that a naive Settings.model_fields[column] lookup gets wrong.
    assert rcd.COLUMN_TO_SETTING["cooldown_base_seconds"] == (
        "dispatcher_rereview_cooldown_seconds"
    )
    assert rcd.COLUMN_TO_SETTING["cooldown_max_seconds"] == (
        "dispatcher_rereview_cooldown_max_seconds"
    )
    assert rcd.COLUMN_TO_SETTING["cooldown_factor"] == "dispatcher_rereview_cooldown_factor"


def test_declared_defaults_serializes_time_to_isoformat():
    # The column is TEXT; usage_cap_config parses it with time.fromisoformat.
    assert Settings.model_fields["key_usage_reset_time_utc"].default == time(4, 0)
    assert rcd.declared_defaults()["key_usage_reset_time_utc"] == "04:00:00"


def test_declared_defaults_omits_none_defaulted_columns():
    assert "key_usage_token_cap" not in rcd.declared_defaults()
    assert "key_usage_token_cap" in rcd.NO_DEFAULT_BY_DESIGN


def test_declared_defaults_reads_class_defaults_not_the_instance(monkeypatch):
    # A stray env var must not change what a boot writes into the database.
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "999.0")
    assert rcd.declared_defaults()["llm_request_timeout_seconds"] == 45.0


def test_declared_defaults_covers_every_mapped_column_except_none_defaults():
    expected = set(rcd.COLUMN_TO_SETTING) - set(rcd.NO_DEFAULT_BY_DESIGN)
    assert set(rcd.declared_defaults()) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runtime_config_defaults.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'review_queue.runtime_config_defaults'`

- [ ] **Step 3: Write minimal implementation**

```python
# review_queue/runtime_config_defaults.py
"""runtime_config's column names tied to the Settings fields they mirror,
and each column's DECLARED default.

Two facts live here that nothing else in the codebase makes explicit:

1. Three columns do not share their Settings field's name -- the cooldown
   trio is `dispatcher_rereview_cooldown_{seconds,max_seconds,factor}` on
   Settings and `cooldown_{base_seconds,max_seconds,factor}` in the table.
   A `Settings.model_fields[column]` lookup silently KeyErrors on them.
2. `key_usage_reset_time_utc` is a `time` on Settings and TEXT in the
   table. The wire format is `time.isoformat()` (always 3-part, "04:00:00"),
   which is what usage_cap_config parses back with `time.fromisoformat`.

THE ONE RULE, same as scripts/gen_docs.py's: read the Settings CLASS's
`model_fields[...].default`, never the module-level `settings` instance.
These values are written into a database at boot. Reading the instance
would let whatever is in this machine's .env/.env.config decide what a
production boot writes -- and would put credential material one attribute
access away from a code path that logs.
"""
from __future__ import annotations

from datetime import time

from pydantic_core import PydanticUndefined

from config import Settings

COLUMN_TO_SETTING: dict[str, str] = {
    "cooldown_base_seconds": "dispatcher_rereview_cooldown_seconds",
    "cooldown_max_seconds": "dispatcher_rereview_cooldown_max_seconds",
    "cooldown_factor": "dispatcher_rereview_cooldown_factor",
    "key_usage_token_cap": "key_usage_token_cap",
    "key_usage_reset_time_utc": "key_usage_reset_time_utc",
    "review_draft_prs": "review_draft_prs",
    "llm_request_timeout_seconds": "llm_request_timeout_seconds",
    "dispatcher_default_retry_after_seconds": "dispatcher_default_retry_after_seconds",
    "dispatcher_failure_base_backoff_seconds": "dispatcher_failure_base_backoff_seconds",
    "dispatcher_failure_max_backoff_seconds": "dispatcher_failure_max_backoff_seconds",
    "dispatcher_max_failure_attempts": "dispatcher_max_failure_attempts",
    "dispatcher_max_notice_post_attempts": "dispatcher_max_notice_post_attempts",
    "dispatcher_min_retry_after_seconds": "dispatcher_min_retry_after_seconds",
    "dispatcher_backoff_jitter_seconds": "dispatcher_backoff_jitter_seconds",
    "dispatcher_notice_sweep_batch_size": "dispatcher_notice_sweep_batch_size",
    "dispatcher_idle_sleep_seconds": "dispatcher_idle_sleep_seconds",
}

# Columns whose declared default is None -- deliberately left NULL rather
# than backfilled. key_usage_token_cap is the only one: a None cap paired
# with a real reset time is "cap intentionally disabled", a valid
# configured state (see usage_cap_config's docstring), not "unset". A
# COALESCE backfill for it would be a no-op anyway; naming it here makes
# that intentional rather than incidental.
NO_DEFAULT_BY_DESIGN: tuple[str, ...] = ("key_usage_token_cap",)


def _wire_value(value: object) -> float | int | bool | str:
    """A declared default as the column's TEXT/numeric/boolean wire form."""
    if isinstance(value, time):
        return value.isoformat()
    return value  # type: ignore[return-value]


def declared_defaults() -> dict[str, float | int | bool | str]:
    """Each mapped column's declared default, in COLUMN_TO_SETTING order,
    omitting every column whose default is None (see NO_DEFAULT_BY_DESIGN).

    A field declared with `default_factory` instead of `default` reads back
    as PydanticUndefined and is omitted too -- there is none today, and a
    future one needs a deliberate decision here rather than a silent
    `PydanticUndefined` reaching a SQL parameter.
    """
    defaults: dict[str, float | int | bool | str] = {}
    for column, field_name in COLUMN_TO_SETTING.items():
        default = Settings.model_fields[field_name].default
        if default is None or default is PydanticUndefined:
            continue
        defaults[column] = _wire_value(default)
    return defaults
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_runtime_config_defaults.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add review_queue/runtime_config_defaults.py tests/test_runtime_config_defaults.py
git commit -m "Extract runtime_config's column-to-setting mapping and declared defaults"
```

---

### Task 2: `cooldown_config.problems()`

`effective_config()` inlines the predicate that decides a cooldown triple is unusable. `deploy.py` hand-reimplements the same comparison, the dashboard has none, and the boot gate never checks it. Extract one definition.

**Files:**
- Modify: `review_queue/cooldown_config.py`
- Test: `tests/test_cooldown_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `cooldown_config.problems(config: dict) -> list[str]`, where `config` has keys `cooldown_base_seconds`, `cooldown_max_seconds`, `cooldown_factor`. Returns human-readable reasons; empty list means usable. Same signature shape as `dispatcher_tuning_config.problems()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cooldown_config.py`:

```python
def test_problems_empty_for_a_usable_triple():
    assert cooldown_config.problems(
        {"cooldown_base_seconds": 300.0, "cooldown_max_seconds": 3600.0,
         "cooldown_factor": 2.0}
    ) == []


def test_problems_accepts_a_base_of_exactly_zero():
    # 0 is immediate re-review with no wait -- valid, per effective_config's
    # docstring. Only a NEGATIVE base is rejected.
    assert cooldown_config.problems(
        {"cooldown_base_seconds": 0.0, "cooldown_max_seconds": 3600.0,
         "cooldown_factor": 2.0}
    ) == []


def test_problems_names_each_unusable_field():
    found = cooldown_config.problems(
        {"cooldown_base_seconds": -1.0, "cooldown_max_seconds": 0.0,
         "cooldown_factor": 0.5}
    )
    joined = "; ".join(found)
    assert "cooldown_base_seconds" in joined
    assert "cooldown_max_seconds" in joined
    assert "cooldown_factor" in joined


def test_problems_reports_base_above_cap():
    found = cooldown_config.problems(
        {"cooldown_base_seconds": 9000.0, "cooldown_max_seconds": 3600.0,
         "cooldown_factor": 2.0}
    )
    assert any("exceeds" in reason for reason in found)


def test_problems_reports_a_missing_or_null_field_as_not_set():
    found = cooldown_config.problems(
        {"cooldown_base_seconds": 300.0, "cooldown_max_seconds": None}
    )
    assert "cooldown_max_seconds is not set" in found
    assert "cooldown_factor is not set" in found


def test_effective_config_delegates_to_problems():
    # One definition of "unusable", not two. A triple problems() rejects
    # must read back as the discarded whole-triple sentinel.
    cooldown_config.set_override_cache(300.0, 3600.0, 0.5)
    assert cooldown_config.problems(
        {"cooldown_base_seconds": 300.0, "cooldown_max_seconds": 3600.0,
         "cooldown_factor": 0.5}
    ) != []
    assert cooldown_config.effective_config() == (None, None, None)
    cooldown_config.reset_override_cache()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cooldown_config.py -v -k problems`
Expected: FAIL — `AttributeError: module 'review_queue.cooldown_config' has no attribute 'problems'`

- [ ] **Step 3: Write minimal implementation**

Add to `review_queue/cooldown_config.py`, above `effective_config()`:

```python
# (key, predicate, description). The single definition of "this cooldown
# triple is unusable", shared by effective_config() below, scripts/deploy.py's
# --sync-config-db guard, dashboard/environment.py's config PATCH, and
# main.py's boot gate -- so no writer can disagree with the reader about
# what it is allowed to store. Mirrors dispatcher_tuning_config._BOUNDS.
#
# A base of exactly 0 is VALID (immediate re-review, no wait); only a
# negative base is rejected. A non-positive cap is not, since it would make
# every escalated wait collapse to it.
_BOUNDS: tuple[tuple[str, Callable[[float], bool], str], ...] = (
    ("cooldown_base_seconds", lambda v: v >= 0, "must be >= 0"),
    ("cooldown_max_seconds", lambda v: v > 0, "must be > 0"),
    ("cooldown_factor", lambda v: v >= 1.0, "must be >= 1.0"),
)

def problems(config: dict) -> list[str]:
    """Every reason `config` is an unusable cooldown triple, as
    human-readable strings. Empty list means usable."""
    found = []
    for key, predicate, description in _BOUNDS:
        if key not in config or config[key] is None:
            found.append(f"{key} is not set")
        elif not predicate(config[key]):
            found.append(f"{key}={config[key]!r} {description}")
    base = config.get("cooldown_base_seconds")
    cap = config.get("cooldown_max_seconds")
    if base is not None and cap is not None and base > cap:
        found.append(f"cooldown base {base} exceeds cooldown max {cap}")
    return found
```

Add `from collections.abc import Callable` to the imports. Then replace `effective_config()`'s inline predicate so there is exactly one definition:

```python
def effective_config() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) as cached -- None values mean "not yet refreshed
    this process", not "use a default": DB is the sole source of truth, per
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
    section 10.4. An override that problems() rejects is discarded as a
    WHOLE triple -- (None, None, None), same as an unrefreshed cache -- so a
    caller can't tell "bad data" from "not refreshed yet" and must treat both
    the same way (defer the ticket, don't guess).

    The predicate itself lives in problems() above, not here, so the writers
    that validate before storing and this reader that discards after reading
    can never drift apart (see that function's comment).
    """
    config = {
        "cooldown_base_seconds": _base,
        "cooldown_max_seconds": _cap,
        "cooldown_factor": _factor,
    }
    if problems(config):
        return (None, None, None)
    return (_base, _cap, _factor)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cooldown_config.py -v`
Expected: PASS — the new tests and every pre-existing one in the file. The pre-existing tests are the real check that `problems()` reproduces the old inline predicate exactly; if any of them fail, the extraction changed behaviour and must be fixed rather than the test adjusted.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green. `tests/test_dispatcher.py` and `tests/test_queue_store.py` exercise `effective_cooldown()`, which reads through `effective_config()` — they are the regression check on this extraction.

- [ ] **Step 6: Commit**

```bash
git add review_queue/cooldown_config.py tests/test_cooldown_config.py
git commit -m "Extract cooldown_config.problems() as the one definition of an unusable triple"
```

---

### Task 3: `usage_cap_config.problems()`

Same extraction for the usage-cap pair. This is the group with **no** validation on any write path today: `dashboard.html:919` is a free-text field, `_apply_config_patch` writes it through unexamined, and a value `time.fromisoformat` cannot parse silently disables the cap.

**Files:**
- Modify: `review_queue/usage_cap_config.py`
- Test: `tests/test_usage_cap_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `usage_cap_config.problems(config: dict) -> list[str]`, where `config` has keys `key_usage_token_cap` (int or None) and `key_usage_reset_time_utc` (str or None).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_usage_cap_config.py`:

```python
def test_problems_empty_for_a_usable_pair():
    assert usage_cap_config.problems(
        {"key_usage_token_cap": 100_000, "key_usage_reset_time_utc": "04:00:00"}
    ) == []


def test_problems_accepts_the_two_part_reset_form():
    assert usage_cap_config.problems(
        {"key_usage_token_cap": 100_000, "key_usage_reset_time_utc": "04:00"}
    ) == []


def test_problems_accepts_a_null_cap_as_intentionally_disabled():
    # A None cap with a real reset time is a valid configured state, not
    # "unset" -- see this module's own docstring.
    assert usage_cap_config.problems(
        {"key_usage_token_cap": None, "key_usage_reset_time_utc": "04:00:00"}
    ) == []


def test_problems_rejects_an_unparseable_reset_time():
    found = usage_cap_config.problems(
        {"key_usage_token_cap": 100_000, "key_usage_reset_time_utc": "4pm"}
    )
    assert any("key_usage_reset_time_utc" in reason for reason in found)


def test_problems_rejects_a_non_positive_cap():
    for cap in (0, -5):
        found = usage_cap_config.problems(
            {"key_usage_token_cap": cap, "key_usage_reset_time_utc": "04:00:00"}
        )
        assert any("key_usage_token_cap" in reason for reason in found), cap


def test_problems_reports_a_missing_reset_time_as_not_set():
    assert "key_usage_reset_time_utc is not set" in usage_cap_config.problems(
        {"key_usage_token_cap": 100_000, "key_usage_reset_time_utc": None}
    )


def test_effective_caps_still_disables_rather_than_raising_on_a_bad_pair():
    # problems() is for WRITERS. The read path's contract is unchanged: it
    # degrades, never raises, so a bad row already in the database cannot
    # take the dispatcher down mid-tick.
    usage_cap_config.set_override_cache(100_000, "4pm")
    assert usage_cap_config.effective_caps() == (None, None)
    usage_cap_config.reset_override_cache()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_usage_cap_config.py -v -k problems`
Expected: FAIL — `AttributeError: module 'review_queue.usage_cap_config' has no attribute 'problems'`

- [ ] **Step 3: Write minimal implementation**

Add to `review_queue/usage_cap_config.py`:

```python
def problems(config: dict) -> list[str]:
    """Every reason `config` is an unusable usage-cap pair, as
    human-readable strings. Empty list means usable.

    For WRITERS -- scripts/deploy.py's --sync-config-db guard,
    dashboard/environment.py's config PATCH, main.py's boot gate. The read
    path (effective_caps below) deliberately does NOT call this: its
    contract is to degrade to (None, None) rather than raise, so a bad row
    already in the database cannot take a dispatcher tick down. This
    function exists so a bad row stops getting written in the first place.

    A None cap is valid ("cap intentionally disabled", see the module
    docstring), so only a non-positive one is rejected -- matching
    config.py's `gt=0` on the same field, which stopped applying the moment
    Settings stopped being this value's runtime source.
    """
    found = []
    cap = config.get("key_usage_token_cap")
    if cap is not None and cap <= 0:
        found.append(f"key_usage_token_cap={cap!r} must be > 0 (or null for no cap)")
    reset = config.get("key_usage_reset_time_utc")
    if reset is None:
        found.append("key_usage_reset_time_utc is not set")
    else:
        try:
            time.fromisoformat(reset)
        except (TypeError, ValueError):
            found.append(
                f"key_usage_reset_time_utc={reset!r} is not an HH:MM or "
                "HH:MM:SS wall-clock time"
            )
    return found
```

`time` is already imported at the top of this module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_usage_cap_config.py -v`
Expected: PASS — the new tests plus every pre-existing one.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add review_queue/usage_cap_config.py tests/test_usage_cap_config.py
git commit -m "Add usage_cap_config.problems() for the writers that had no validation"
```

---

### Task 4: Generalize the missing-column and ALTER helpers to any table

`deploy.py`'s two helpers are hardcoded to `runtime_config`. `slot_config` has the identical hazard — `store.get_slot_config()` SELECTs `model, vertex_gcp_project, vertex_gcp_location` by name (`store.py:998`) and `set_slot_config()` INSERTs them (`store.py:1027`), so a narrower provisioner-created table raises `UndefinedColumn` from the bot's own read path. Generalize now; Task 5 uses both.

**Files:**
- Modify: `scripts/deploy.py` (`_missing_runtime_config_columns`, `_runtime_config_alter_statements`)
- Modify: `review_queue/store.py` (expose `SLOT_CONFIG_COLUMNS`)
- Test: `tests/test_deploy_script.py`, `tests/test_store_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `store.SLOT_CONFIG_COLUMNS: tuple[tuple[str, str], ...]` — `(name, SQL type + constraints)` for `slot_config`, in DDL order, with `_SCHEMA` built from it exactly as `runtime_config` already is.
  - `deploy._missing_columns(table: str, columns: tuple[tuple[str, str], ...]) -> list[str] | None` — names absent from the live table, in declared order; `None` if the table itself does not exist.
  - `deploy._alter_statements(table: str, columns: tuple[tuple[str, str], ...], missing: list[str]) -> str` — one `ALTER TABLE <table> ADD COLUMN IF NOT EXISTS ...;` line per missing name.
  - `_missing_runtime_config_columns()` and `_runtime_config_alter_statements(missing)` remain as thin `runtime_config`-bound wrappers, so `check_runtime_config_schema()` and its existing tests are untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store_schema.py`:

```python
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
```

Append to `tests/test_deploy_script.py`:

```python
def test_alter_statements_generalize_to_any_table():
    columns = (("alpha", "TEXT"), ("beta", "INTEGER NOT NULL DEFAULT 0"))
    sql = deploy._alter_statements("some_table", columns, ["beta"])
    assert sql == (
        "ALTER TABLE some_table ADD COLUMN IF NOT EXISTS beta INTEGER NOT NULL DEFAULT 0;"
    )


def test_runtime_config_alter_statements_still_wraps_the_generic_helper():
    sql = deploy._runtime_config_alter_statements(["review_draft_prs"])
    assert sql == (
        "ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS review_draft_prs BOOLEAN;"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_schema.py tests/test_deploy_script.py -v -k "slot_config_columns or nullable_or_defaulted or generalize or still_wraps"`
Expected: FAIL — `ImportError: cannot import name 'SLOT_CONFIG_COLUMNS'` and `AttributeError: module 'scripts.deploy' has no attribute '_alter_statements'`

- [ ] **Step 3: Write minimal implementation**

In `review_queue/store.py`, add above `_SCHEMA` and rebuild the `slot_config` block from it:

```python
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
```

Then replace the literal `slot_config` block inside `_SCHEMA` with the same generated form `runtime_config` already uses:

```python
CREATE TABLE IF NOT EXISTS slot_config (
""" + ",\n".join(
    f"    {name:<25} {sql_type}" for name, sql_type in SLOT_CONFIG_COLUMNS
) + """,
    PRIMARY KEY (provider, slot_index)
);
ALTER TABLE slot_config ENABLE ROW LEVEL SECURITY;
```

In `scripts/deploy.py`, add the generic helpers and reduce the existing two to wrappers:

```python
def _missing_columns(
    table: str, columns: tuple[tuple[str, str], ...]
) -> list[str] | None:
    """Names from `columns` absent from the live `table`, in declared order
    -- or None if the table itself doesn't exist yet, a distinct situation
    from "every column is missing".

    Raises psycopg.Error on a connection failure -- callers already have
    their own way of reporting that.
    """
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
        if (row[0] if row else None) is None:
            return None
        live = {
            name
            for (name,) in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            ).fetchall()
        }
    return [name for name, _sql_type in columns if name not in live]


def _alter_statements(
    table: str, columns: tuple[tuple[str, str], ...], missing: list[str]
) -> str:
    """One `ALTER TABLE <table> ADD COLUMN IF NOT EXISTS` line per name in
    `missing`, in the type each is declared with in `columns`.

    Only ever ADDs. A NOT NULL column with no DEFAULT cannot be added to a
    table that already has rows -- tests/test_store_schema.py pins that
    every declared column is nullable, defaulted, or provisioner-written.
    """
    by_name = dict(columns)
    return "\n".join(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {by_name[name]};"
        for name in missing
    )


def _missing_runtime_config_columns() -> list[str] | None:
    """runtime_config's missing columns -- see _missing_columns(). Kept as a
    named wrapper because check_runtime_config_schema() and its operator-facing
    message are runtime_config-specific."""
    return _missing_columns("runtime_config", store.RUNTIME_CONFIG_COLUMNS)


def _runtime_config_alter_statements(missing: list[str]) -> str:
    """The ready-to-run fix for runtime_config -- see _alter_statements()."""
    return _alter_statements("runtime_config", store.RUNTIME_CONFIG_COLUMNS, missing)
```

Note the `information_schema` query is now parameterized on `table_name`; keep the `to_regclass` parameter bound too, never interpolated.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_schema.py tests/test_deploy_script.py -v`
Expected: PASS. `test_deploy_script.py`'s existing `check_runtime_config_schema` tests (around lines 967-1060) are the regression check that the wrappers behave identically.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green. `tests/test_store_slot_config.py` is the check that the regenerated `slot_config` DDL is byte-equivalent in effect.

- [ ] **Step 6: Commit**

```bash
git add review_queue/store.py scripts/deploy.py tests/test_store_schema.py tests/test_deploy_script.py
git commit -m "Generalize the missing-column and ALTER helpers to any table, add SLOT_CONFIG_COLUMNS"
```

---

### Task 5: `init_pool()` widens both live tables

**Files:**
- Modify: `review_queue/store.py` (`init_pool`)
- Test: `tests/test_store_init.py`

**Interfaces:**
- Consumes: `store.SLOT_CONFIG_COLUMNS` and `deploy._alter_statements` (Task 4). To avoid `store` importing `scripts.deploy` (a CLI module that imports `store` — a cycle), `init_pool()` builds its own ALTER text from the same tuples via a small private helper in `store.py`; Task 4's `deploy` helpers stay the operator-facing path.
- Produces: `store._widen_statements(conn) -> list[str]` — the ALTER statements this boot needs, for the log line in Task 6.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store_init.py`:

```python
import pytest


@pytest.mark.db
def test_init_pool_widens_a_narrower_pre_provisioned_runtime_config(db_url, db_exec):
    """The wizard-provisioning chronology: something else created the table
    first, narrower than this repo declares. CREATE TABLE IF NOT EXISTS is a
    no-op against it, so init_pool() must ADD the missing columns or the
    bot's own INSERT/SELECT hits UndefinedColumn."""
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
            for (name,) in db_exec(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'runtime_config'",
                fetch=True,
            )
        }
    finally:
        store.close_pool()
    declared = {name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS}
    assert declared <= live, f"init_pool did not widen: {sorted(declared - live)}"


@pytest.mark.db
def test_init_pool_widens_a_narrower_pre_provisioned_slot_config(db_url, db_exec):
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
def test_init_pool_widening_is_idempotent(db_url, db_exec):
    db_exec("DROP TABLE IF EXISTS runtime_config, slot_config CASCADE")
    store.init_pool()
    store.close_pool()
    store.init_pool()  # second boot: every column already present
    store.close_pool()
```

Check `tests/conftest.py` for the exact `db_exec` fixture signature before writing these; if it does not support a `fetch=True` form, use the fixture's own read helper instead and keep the assertion identical.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_init.py -v -k widen`
Expected: FAIL — `psycopg.errors.UndefinedColumn` on the `slot_config` case and a non-empty `declared - live` set on the `runtime_config` case.

- [ ] **Step 3: Write minimal implementation**

In `review_queue/store.py`, add the private helper and call it from `init_pool()`:

```python
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
```

In `init_pool()`, replace the `with _pool.connection() as conn: conn.execute(_SCHEMA)` body with:

```python
        with _pool.connection() as conn:
            conn.execute(_SCHEMA)
            for statement in _widen_statements(conn):
                conn.execute(statement)
```

The pool is configured with `dict_row` (`_configure`), so `row["column_name"]` is the right accessor — confirm against `_configure` before writing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_init.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add review_queue/store.py tests/test_store_init.py
git commit -m "Widen a narrower pre-provisioned runtime_config/slot_config at boot"
```

---

### Task 6: `init_pool()` backfills NULL columns from declared defaults

**Files:**
- Modify: `review_queue/store.py` (`init_pool`, module imports, docstring)
- Test: `tests/test_store_init.py`

**Interfaces:**
- Consumes: `runtime_config_defaults.declared_defaults()` (Task 1), `store._widen_statements` (Task 5).
- Produces: `store._backfill_runtime_config(conn) -> list[str]` — names of the columns this boot actually filled.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store_init.py`:

```python
@pytest.mark.db
def test_init_pool_backfills_null_columns_from_declared_defaults(db_url, db_exec, caplog):
    """The 2026-09-09 chronology, fixed: a provisioner writes the row with
    only provider/key_index/updated_at, then this service boots. Every
    tuning column it left NULL must come back filled, so the boot gate
    passes and the dispatcher has a usable config."""
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
def test_init_pool_backfill_never_overwrites_an_existing_value(db_url, db_exec):
    """COALESCE, not ON CONFLICT DO NOTHING and not an unconditional write:
    a value a provisioner, the CLI, or the dashboard deliberately set must
    survive every subsequent boot untouched."""
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
def test_init_pool_backfill_creates_the_row_when_absent(db_url, db_exec, caplog):
    """No provisioner ran at all: the row must be created with defaults, so
    the only thing main.py's gate then complains about is `provider`, which
    the bot genuinely cannot invent. This is the loudest case, not the
    quietest -- every backfillable column is reported as filled."""
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
```

Add `from review_queue import dispatcher_tuning_config` to the test module's imports if absent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_init.py -v -k backfill`
Expected: FAIL — `problems(config)` returns nine "is not set" entries.

- [ ] **Step 3: Write minimal implementation**

Add to `review_queue/store.py`'s imports:

```python
import logging

from review_queue import cooldown_config, runtime_config_defaults

logger = logging.getLogger(__name__)
```

(`cooldown_config` is already imported; extend that line rather than duplicating it.)

Add the backfill:

```python
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
```

Note: `updated_at` is written on INSERT (the column is `NOT NULL`) but deliberately **not** in the `DO UPDATE SET` list — a backfill is not a configuration change and must not restamp a row it may not have altered.

In `init_pool()`, after the widening loop:

```python
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
```

Then rewrite `init_pool()`'s "Deliberately does NOT seed" paragraph to lead with the `DO NOTHING` versus `COALESCE` distinction — a reader who finds bot-side defaulting here and remembers `ISSUES.md` 2026-09-09 must be told immediately why this is not that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_init.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green. Watch `tests/test_main_lifespan.py` — tests that assert the boot gate *fails* on an incomplete row may now find a backfilled one. Any such test must be reworked to assert the new behaviour (the gate fails only on `provider`/`slot_config`, or on a present-but-invalid value), not deleted.

- [ ] **Step 6: Commit**

```bash
git add review_queue/store.py tests/test_store_init.py tests/test_main_lifespan.py
git commit -m "Backfill NULL runtime_config columns at boot with COALESCE, never overwriting"
```

---

### Task 7: The boot gate validates all three field groups

**Files:**
- Modify: `main.py` (lifespan, around lines 129-141)
- Test: `tests/test_main_lifespan.py`

**Interfaces:**
- Consumes: `cooldown_config.problems` (Task 2), `usage_cap_config.problems` (Task 3), the backfill (Task 6).
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_lifespan.py`, following the file's existing lifespan-test pattern:

```python
@pytest.mark.db
async def test_lifespan_refuses_to_start_on_an_invalid_cooldown_triple(
    db_url, db_exec, boot_ready_runtime_config
):
    """Backfill only fills NULLs. A present-but-invalid value -- reachable
    from the dashboard's config panel before this change -- must surface as
    a boot failure, not survive reboots invisibly while every re-review
    defers."""
    store.init_pool()
    store.set_cooldown_override(300.0, 3600.0, 0.5, "2026-09-10T00:00:00+00:00")
    store.close_pool()

    with pytest.raises(RuntimeError, match="cooldown_factor"):
        async with lifespan(FastAPI()):
            pass


@pytest.mark.db
async def test_lifespan_refuses_to_start_on_an_unparseable_reset_time(
    db_url, db_exec, boot_ready_runtime_config
):
    store.init_pool()
    store.set_usage_cap_override(100_000, "4pm", "2026-09-10T00:00:00+00:00")
    store.close_pool()

    with pytest.raises(RuntimeError, match="key_usage_reset_time_utc"):
        async with lifespan(FastAPI()):
            pass


@pytest.mark.db
async def test_lifespan_starts_with_a_null_token_cap(
    db_url, db_exec, boot_ready_runtime_config
):
    """A null cap is "intentionally disabled", a valid configured state --
    it must not be mistaken for an incomplete row."""
    store.init_pool()
    store.set_usage_cap_override(None, "04:00:00", "2026-09-10T00:00:00+00:00")
    store.close_pool()

    async with lifespan(FastAPI()):
        pass
```

Reuse the file's existing fixture for a boot-ready database rather than inventing one; if it is named differently, use that name and keep the assertions identical.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_main_lifespan.py -v -k "cooldown_triple or reset_time or null_token_cap"`
Expected: FAIL — no `RuntimeError` is raised; the lifespan starts happily on both bad configs.

- [ ] **Step 3: Write minimal implementation**

In `main.py`, replace the single tuning-knob check with all three groups:

```python
    # Backfill (store.init_pool) fills what this repo can derive; it cannot
    # fix a value that is PRESENT and invalid. Those reach the database from
    # the dashboard's config panel, from an older release, or by hand -- and
    # every one of them degrades silently at read time rather than raising:
    # dispatcher_tuning_config raises TuningConfigUnavailable per tick,
    # cooldown_config discards the whole triple (every re-review defers),
    # usage_cap_config discards the whole pair (the cap fails OPEN). None of
    # those is visible at boot, which is why all three are checked here, at
    # the same fail-loudly boundary as provider/slot_config above.
    _config_problems = [
        *dispatcher_tuning_config.problems(store.get_dispatcher_tuning_config()),
        *cooldown_config.problems(
            dict(
                zip(
                    ("cooldown_base_seconds", "cooldown_max_seconds", "cooldown_factor"),
                    store.get_cooldown_overrides(),
                )
            )
        ),
        *usage_cap_config.problems(
            dict(
                zip(
                    ("key_usage_token_cap", "key_usage_reset_time_utc"),
                    store.get_usage_cap_overrides(),
                )
            )
        ),
    ]
    if _config_problems:
        raise RuntimeError(
            "runtime_config is missing or invalid -- refusing to start: "
            + "; ".join(_config_problems)
            + ". Set them with `uv run python -m scripts.deploy --sync-config-db` "
            "or via the dashboard's config panel."
        )
```

Add `cooldown_config` and `usage_cap_config` to `main.py`'s `from review_queue import ...` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_lifespan.py
git commit -m "Validate the cooldown and usage-cap groups at boot, not just the tuning knobs"
```

---

### Task 8: `--sync-config-db` uses the shared predicates and the shared mapping

**Files:**
- Modify: `scripts/deploy.py` (`sync_config_db`, `_DB_SYNCED_COLUMNS`)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `cooldown_config.problems`, `usage_cap_config.problems`, `runtime_config_defaults.COLUMN_TO_SETTING`.
- Produces: no new symbols. `_DB_SYNCED_COLUMNS` keeps its name and its order.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deploy_script.py`:

```python
def test_sync_config_db_refuses_an_unparseable_reset_time(monkeypatch, capsys):
    """The usage-cap pair had NO guard on this path -- only Settings' own
    `time` coercion, which an already-stored bad value bypasses entirely."""
    monkeypatch.setattr(deploy.settings, "key_usage_reset_time_utc", "4pm", raising=False)
    assert deploy.sync_config_db() == 2
    assert "key_usage_reset_time_utc" in capsys.readouterr().err


def test_sync_config_db_cooldown_guard_uses_the_shared_predicate(monkeypatch, capsys):
    monkeypatch.setattr(deploy.settings, "dispatcher_rereview_cooldown_factor", 0.5)
    assert deploy.sync_config_db() == 2
    err = capsys.readouterr().err
    assert "cooldown_factor" in err


def test_db_synced_columns_are_all_in_the_shared_mapping():
    from review_queue import runtime_config_defaults as rcd

    unmapped = set(deploy._DB_SYNCED_COLUMNS) - set(rcd.COLUMN_TO_SETTING)
    assert not unmapped, (
        "a --sync-config-db column with no COLUMN_TO_SETTING entry: "
        f"{sorted(unmapped)}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -v -k "unparseable_reset_time or shared_predicate or shared_mapping"`
Expected: FAIL — no reset-time guard exists, and the cooldown message uses the old hand-written wording.

- [ ] **Step 3: Write minimal implementation**

In `sync_config_db()`, replace the hand-written cooldown guard with the shared predicate and add the usage-cap one. Build the seed dict from `COLUMN_TO_SETTING` so the CLI and the backfill cannot disagree about which column maps to which field:

```python
    seed = {
        column: getattr(settings, field_name)
        for column, field_name in runtime_config_defaults.COLUMN_TO_SETTING.items()
        if column in _DB_SYNCED_COLUMNS
    }
    seed["key_usage_reset_time_utc"] = settings.key_usage_reset_time_utc.isoformat()

    invalid = [
        *cooldown_config.problems(seed),
        *usage_cap_config.problems(seed),
        *dispatcher_tuning_config.problems(seed),
    ]
    if invalid:
        print(
            "refusing to sync: .env.config would write an unusable config -- "
            + "; ".join(invalid)
            + " -- fix .env.config first",
            file=sys.stderr,
        )
        return 2
    if settings.dispatcher_idle_sleep_seconds <= 0:
        print(
            "refusing to sync: DISPATCHER_IDLE_SLEEP_SECONDS="
            f"{settings.dispatcher_idle_sleep_seconds} would busy-loop the dispatcher "
            "(must be > 0) -- fix .env.config first",
            file=sys.stderr,
        )
        return 2
    wanted = tuple(seed[column] for column in _DB_SYNCED_COLUMNS)
```

`settings` (the instance) is correct here and must stay — this command's whole job is to push `.env.config`'s resolved values. That is the deliberate difference from Task 1's class-default rule, and it deserves a comment saying so.

Keep the `_looks_like_local_test_db` guard first, before anything else, exactly as it is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: PASS. The existing `--sync-config-db` tests (around lines 2140-2250) are the regression check that the mapping refactor preserved column order and reporting.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "Route --sync-config-db through the shared predicates and column mapping"
```

---

### Task 9: The dashboard PATCH validates before writing, and the store docstrings stop lying

The last unguarded writer. `_apply_config_patch` already does this correctly for the 9 tuning knobs and says why: "this endpoint is the ONLY place a bad value can be caught before it reaches the dispatcher." Extend that to the two groups it skipped.

**Files:**
- Modify: `dashboard/environment.py` (`_apply_config_patch`)
- Modify: `review_queue/store.py` (`set_cooldown_override`, `set_usage_cap_override` docstrings)
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `cooldown_config.problems`, `usage_cap_config.problems`.
- Produces: no new symbols. Failure entries keep the endpoint's existing `{"key": ..., "error": ...}` shape.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/tests/test_environment.py`:

```python
@pytest.mark.db
async def test_config_patch_rejects_an_unparseable_reset_time(client):
    """Before this, "4pm" was stored and silently disabled the usage cap
    forever, with a 200 and the field reported as applied."""
    resp = await client.patch(
        "/api/environment/config", json={"usage_cap_reset": "4pm"}
    )
    body = resp.json()
    assert "usage_cap_reset" not in body["applied"]
    assert any(f["key"] == "usage_cap_reset" for f in body["failed"])


@pytest.mark.db
async def test_config_patch_rejects_a_non_positive_token_cap(client):
    resp = await client.patch(
        "/api/environment/config", json={"usage_cap_tokens": 0}
    )
    body = resp.json()
    assert "usage_cap_tokens" not in body["applied"]
    assert any(f["key"] == "usage_cap_tokens" for f in body["failed"])


@pytest.mark.db
async def test_config_patch_rejects_a_cooldown_factor_below_one(client):
    """effective_config() discards the whole triple on factor < 1, and its
    callers must defer -- so this silently stalled every re-review."""
    resp = await client.patch(
        "/api/environment/config", json={"cooldown_factor": 0.5}
    )
    body = resp.json()
    assert "cooldown_factor" not in body["applied"]
    assert any(f["key"] == "cooldown_factor" for f in body["failed"])


@pytest.mark.db
async def test_config_patch_rejects_a_partial_cooldown_that_breaks_the_triple(client):
    """The merge is against CURRENT values, so a single field can make the
    stored triple unusable. Validation must run on the merged result, not
    on the submitted field alone."""
    await client.patch(
        "/api/environment/config",
        json={"cooldown_base_seconds": 300.0, "cooldown_max_seconds": 3600.0,
              "cooldown_factor": 2.0},
    )
    resp = await client.patch(
        "/api/environment/config", json={"cooldown_base_seconds": 9000.0}
    )
    body = resp.json()
    assert "cooldown_base_seconds" not in body["applied"]


@pytest.mark.db
async def test_config_patch_still_accepts_a_null_token_cap(client):
    resp = await client.patch(
        "/api/environment/config", json={"usage_cap_tokens": None}
    )
    assert "usage_cap_tokens" in resp.json()["applied"]
```

Match the file's existing fixture names and async style; if its client fixture is named differently, use that name.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_environment.py -v -k "unparseable or non_positive_token or factor_below_one or partial_cooldown"`
Expected: FAIL — all four bad values are reported in `applied`.

- [ ] **Step 3: Write minimal implementation**

In `_apply_config_patch`, wrap each of the two blocks in a merged-group validation, mirroring the tuning block's existing shape:

```python
    cooldown_keys = ("cooldown_base_seconds", "cooldown_max_seconds", "cooldown_factor")
    cooldown_fields = {k: fields[k] for k in cooldown_keys if k in fields}
    if cooldown_fields:
        try:
            current_base, current_cap, current_factor = store.get_cooldown_overrides()
            merged = {
                "cooldown_base_seconds": cooldown_fields.get(
                    "cooldown_base_seconds", current_base),
                "cooldown_max_seconds": cooldown_fields.get(
                    "cooldown_max_seconds", current_cap),
                "cooldown_factor": cooldown_fields.get(
                    "cooldown_factor", current_factor),
            }
            # Validated as a whole group against the MERGED result, not the
            # submitted field alone: this endpoint merges partial input with
            # current values, so one field can make the stored triple
            # unusable on its own. Rejected together and never partially
            # written -- the same shape the 9 tuning knobs below use, and for
            # the same reason (cooldown_config.effective_config() discards an
            # invalid triple whole, which makes every re-review defer with no
            # boot-time signal).
            invalid = cooldown_config.problems(merged)
            if invalid:
                failed.extend(
                    {"key": k, "error": "; ".join(invalid)} for k in cooldown_fields
                )
            else:
                store.set_cooldown_override(
                    merged["cooldown_base_seconds"],
                    merged["cooldown_max_seconds"],
                    merged["cooldown_factor"],
                    now,
                )
                applied.extend(cooldown_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in cooldown_fields)
```

And the usage block, noting that this endpoint's field names differ from the column names:

```python
    usage_keys = ("usage_cap_tokens", "usage_cap_reset")
    usage_fields = {k: fields[k] for k in usage_keys if k in fields}
    if usage_fields:
        try:
            current_tokens, current_reset = store.get_usage_cap_overrides()
            tokens = usage_fields.get("usage_cap_tokens", current_tokens)
            reset = usage_fields.get("usage_cap_reset", current_reset)
            # This endpoint's field names are usage_cap_*; the predicate is
            # keyed by COLUMN name, so translate rather than duplicating it.
            invalid = usage_cap_config.problems(
                {"key_usage_token_cap": tokens, "key_usage_reset_time_utc": reset}
            )
            if invalid:
                failed.extend(
                    {"key": k, "error": "; ".join(invalid)} for k in usage_fields
                )
            else:
                store.set_usage_cap_override(tokens, reset, now)
                applied.extend(usage_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in usage_fields)
```

Add `cooldown_config` and `usage_cap_config` to `dashboard/environment.py`'s `review_queue` imports.

Then correct both `store.py` docstrings. Each currently claims its "only caller, `scripts/deploy.py::sync_config_db()`, always writes the full triple/pair straight from `.env.config`'s resolved `Settings` values -- there is no partial-field write to merge with a current value for." That is false and is exactly what justified having no validation. Replace with the two real callers, the fact that the dashboard *does* merge partial input, and the rule that validation is the caller's job via the shared predicate:

```python
    """... Two callers: scripts/deploy.py::sync_config_db(), which writes
    the full triple from .env.config's resolved Settings values, and
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS, including the pre-existing `test_config_patch` tests at around line 169.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add dashboard/environment.py review_queue/store.py dashboard/tests/test_environment.py
git commit -m "Validate cooldown and usage-cap writes in the dashboard PATCH before storing"
```

---

## Done criteria

- [ ] `uv run pytest -v` fully green; `uv run ruff check .` clean.
- [ ] A database whose `runtime_config` has only `id`/`provider`/`*_key_index`/`updated_at` boots successfully, and logs a WARNING naming the columns it filled.
- [ ] A `runtime_config`/`slot_config` narrower than this repo declares is widened at boot rather than raising `UndefinedColumn`.
- [ ] A value the wizard, `--sync-config-db`, or the dashboard deliberately wrote survives a boot untouched.
- [ ] `cooldown_factor = 0.5`, `usage_cap_reset = "4pm"`, and `usage_cap_tokens = 0` are all rejected by both the CLI and the dashboard, and a stored one fails the boot gate.
- [ ] `store.py`'s two "only caller" docstrings name both callers and point at the shared predicate.
- [ ] **Not done here:** no push, and no `dashboard/static/` change — the client-side `min`/`pattern` attributes are deliberately out of scope (spec §4.4) and need the `ui-visual-review` skill in their own session.
