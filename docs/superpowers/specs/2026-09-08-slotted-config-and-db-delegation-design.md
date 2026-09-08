# Per-slot Vertex config + delegating more operational vars to the DB

## 1. Background

Two threads from the 2026-09-08 env-var rename work, folded into one spec
per the user's request:

- **The deferred slot-suffix gap**: `VERTEX_GCP_PROJECT`/`VERTEX_GCP_LOCATION`
  are single, flat values today, but a Vertex credential slot
  (`VERTEX_GCP_SERVICE_ACCOUNT_KEY_1`, `_2`, ...) can belong to a different
  GCP project/region than slot 0. `*_MODEL` vars have the same shape problem:
  `_fetch_models_for_provider` already takes a `slot` param because different
  credentials can see different model catalogs, yet only one model override
  exists per provider, not per slot.
- **DB-delegation research**: `scripts/deploy.py`'s `_GENERIC_OPERATIONAL_ENV_ATTRS`
  (11 vars, still pushed to Render, still requiring a redeploy to change) has
  9 pure dispatcher/timeout tuning knobs with no secret content, directly
  analogous in kind to the 6 vars already living DB-only
  (`_DB_SYNCED_OPERATIONAL_KEYS`: the cooldown trio, usage-cap pair,
  `REVIEW_DRAFT_PRS`) via the established cache-refresh pattern
  (`review_queue/cooldown_config.py` et al.).

## 2. Goal

1. Make Vertex's project/location, and each provider's model, resolvable
   **per credential slot**, not just per provider.
2. Move the 9 dispatcher/timeout tuning knobs (plus `VERTEX_GCP_PROJECT`/
   `VERTEX_GCP_LOCATION` themselves) off Render entirely — DB-only, editable
   with no redeploy, exactly like the existing 6.

## 3. Explicitly out of scope

- **`GITHUB_TARGET_REPO`** — a real DB-delegation candidate too (see the
  prior analysis), but it's in `_ALWAYS_SYNCED` and `main.py`'s lifespan
  hard-refuses to boot without it set — moving it would mean restructuring a
  boot-time required-check to read the DB instead of `Settings`, a
  materially bigger, separate change. Not in this spec.
- **Credentials** (`GEMINI_API_KEY`, `GROQ_API_KEY`,
  `VERTEX_GCP_SERVICE_ACCOUNT_KEY`[+slots]) — never DB-stored, per this
  project's secret-handling rules. Only *which slot* is active, and that
  slot's non-secret project/location/model, move to the DB.
- Structural/boot vars (`DATABASE_URL`, `RENDER_SERVICE_NAME`,
  `PUBLIC_BASE_URL`, `GITHUB_APP_ID`/`_INSTALLATION_ID`/`_PRIVATE_KEY`,
  `GITHUB_WEBHOOK_SECRET`, `DASHBOARD_*`, `RENDER_API_KEY`,
  `UPTIMEROBOT_API_KEY`) — unchanged.

## 4. Per-slot config: schema + resolution

### 4a. New table, not more flat columns

`runtime_config` (one singleton row) already has `vertex_key_index`/
`vertex_model` as flat per-provider columns. Slotting multiplies the
dimension (provider × slot × field), which doesn't fit a singleton-row
table — a new table instead:

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

`vertex_gcp_project`/`vertex_gcp_location` are only ever populated/read when
`provider = 'vertex'`; NULL for gemini/groq rows. **Open question for you**:
is per-slot `model` actually wanted for gemini/groq too, or is the real gap
just Vertex's project/location (the only fields that generalize your
original `VERTEX_GCP_LOCATION` example)? I've scoped the table to cover all
three since the model-catalog-per-slot fetch already exists, but this is the
one place I'd narrow scope if that's not what you had in mind — it'd shrink
to `vertex_slot_config(slot_index, gcp_project, gcp_location)` with model
staying provider-flat as it is today.

### 4b. Resolution: already 90% wired

`providers/factory.py::get_provider()` already resolves `index =
key_index.active_key_index(provider)` **before** calling `_build(provider,
index, model)` — the exact per-slot join point already exists, it just isn't
consulted for model/project/location yet:

- Widen `providers/active_model.py`'s cache from `dict[str, str]` (by
  provider) to `dict[tuple[str, int], str]` (by provider+slot). Its
  accessor becomes `active_model(provider, index)`, falling back to the
  provider-flat DB override (unchanged), then the env default — a third
  fallback tier, not a replacement for the existing two.
- New `providers/active_vertex_slot.py` (mirrors `active_model.py`/
  `key_index.py` exactly): `active_vertex_project(index) -> str`,
  `active_vertex_location(index) -> str`, each falling back to
  `settings.vertex_gcp_project`/`vertex_gcp_location` (which become DB-only
  per §5, so really: per-slot DB override → flat DB override → env default).
- `factory.py::_build`'s vertex branch already receives `index` — swap
  `settings.vertex_gcp_project`/`settings.vertex_gcp_location` for
  `active_vertex_slot.active_vertex_project(index)`/`active_vertex_location(index)`.
- `dispatcher.py::process_next_due` gains one more refresh call, same
  cadence and fail-safe shape as `_refresh_model_overrides` (degrade to
  `reset_override_cache()` on any failure).
- `dashboard/environment.py`'s guided-setup apply flow
  (`_apply_llm_credential`) gains `slot`-scoped project/location fields
  alongside its existing per-slot model field.

## 5. DB-only migration: 9 tuning knobs + Vertex project/location

Same established pattern as `cooldown_config.py`: a `runtime_config` column
+ a tiny cache module + a refresh call in `process_next_due`.

| Var | Read site (already inside `process_next_due`'s call chain) |
|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | `providers/groq.py`, `providers/google_genai.py` |
| `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS` | `providers/groq.py`, `providers/google_genai.py` |
| `DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS` | `dispatcher.py` (backoff calc) |
| `DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS` | `dispatcher.py` (backoff calc) |
| `DISPATCHER_MAX_FAILURE_ATTEMPTS` | `dispatcher.py` (terminal-failure check) |
| `DISPATCHER_MAX_NOTICE_POST_ATTEMPTS` | `dispatcher.py` |
| `DISPATCHER_MIN_RETRY_AFTER_SECONDS` | `dispatcher.py` |
| `DISPATCHER_BACKOFF_JITTER_SECONDS` | `dispatcher.py` |
| `DISPATCHER_NOTICE_SWEEP_BATCH_SIZE` | `store.py`'s notice sweep (already a DB round-trip inside `post_pending_notices`, called every `run_forever` iteration — piggyback the refresh there, no new query) |

`VERTEX_GCP_PROJECT`/`VERTEX_GCP_LOCATION` migrate the same way (flat DB
override, §4b's second fallback tier) — refreshed alongside the new
`slot_config` refresh in the same `process_next_due` call.

**`DISPATCHER_IDLE_SLEEP_SECONDS` is the one exception**, both here and in
the per-slot work: it's read in `run_forever()`'s main loop *specifically
when no ticket was claimed* — the per-claimed-ticket refresh literally never
fires while the queue is idle, which is exactly when this value matters.
Needs its own throttled refresh in the main loop itself (e.g. only re-query
if more than N seconds have elapsed since the last refresh), not a refresh
on every idle iteration — that would be a DB hit every single poll tick.

Once migrated: remove all 11 (9 knobs + project + location) from
`scripts/deploy.py`'s `_GENERIC_OPERATIONAL_ENV_ATTRS`, add them to
`_DB_SYNCED_OPERATIONAL_KEYS`, and drop their `render.yaml`/`.env.example`
entries (matching how the existing 6 already have none).

## 6. Dashboard implications

The config panel (`dashboard/environment.py` + `dashboard/static/
dashboard.html`'s `#configForm`) already has info-hover + alphabetical
sort for the 7 existing tunables (from the 2026-09-08 sort/prettify work).
This adds:

- 9 more flat fields (the tuning knobs) to that same form, same treatment
  (info hover via a `CONFIG_FIELD_ENV_VAR`-style mapping, alphabetized
  alongside the existing 7).
- A genuinely new UI surface for per-slot editing: today's
  `provider-model-grid` shows one row per *provider*; per-slot editing needs
  one row per *(provider, slot actually provisioned)* with project/location/
  model inputs — closer in shape to the guided-setup modal's per-credential
  fields than to the existing flat config rows. This is the biggest single
  piece of new work in this spec and probably deserves its own task-level
  design pass (mockup/interaction question) once the rest of this spec is
  approved, rather than being fully speced here.

## 7. Open decisions for you to confirm or correct

1. §4a: per-slot `model` for all three providers, or just Vertex
   project/location (narrower table)?
2. Is the `slot_config` table name/shape acceptable, or would you rather
   extend `runtime_config` some other way (e.g. one row per slot instead of
   a separate table)?
3. §6: is the per-slot dashboard UI in scope for the same implementation
   pass as §4b/§5's backend changes, or a follow-up once the backend lands?
