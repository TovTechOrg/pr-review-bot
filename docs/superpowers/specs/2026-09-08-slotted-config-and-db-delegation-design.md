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

### 4a. New table, not more flat columns — durable per slot, independent of which slot is active

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

**Scope confirmed: per-slot `model` for all three providers**, not just
Vertex's project/location — no schema change from the draft above, since
`provider` is already a column rather than the table being Vertex-shaped.
`vertex_gcp_project`/`vertex_gcp_location` stay NULL for gemini/groq rows
(only meaningful for `provider = 'vertex'`).

**Persistence, made explicit** (this is what the first draft under-specified):
a `slot_config` row is durable and completely independent of which slot is
currently *active*. "Which slot is active" is a separate concern, unchanged
from today — it stays in `runtime_config.vertex_key_index`/
`gemini_key_index`/`groq_key_index`. Switching that pointer (via the config
panel or `scripts/set_override.py`) **never reads or writes `slot_config`
at all** — it only changes which row later resolution joins against.
Concretely:

- Configure slot 2 (model, and for vertex, project/location) once, via
  guided-setup apply.
- Switch the active slot to 0, then later back to 2, any number of times.
- Slot 2's row in `slot_config` is untouched the whole time — no
  respecification needed on switching back, because switching back never
  touched it in the first place.
- The *only* way a `slot_config` row changes is an explicit edit to that
  specific slot (guided-setup apply targeting that slot, or a future direct
  edit in the config panel) — never as a side effect of `key_index`
  changing.

### 4b. Resolution — DB is the sole source of truth, no env fallback

`providers/factory.py::get_provider()` already resolves `index =
key_index.active_key_index(provider)` **before** calling `_build(provider,
index, model)` — the exact per-slot join point already exists, it just isn't
consulted for model/project/location yet. But the resolution shape itself
changes from what the first draft proposed:

**No env-default fallback tier, anywhere in this spec.** The first draft's
"per-slot DB override → flat DB override → env default" chain was borrowed
directly from the *already-shipped* pattern in `active_model.py`/
`cooldown_config.py`: when there's no DB override, those modules fall back
to whatever `Settings.<field>` resolves to — a real `.env`/`.env.config`/
Render value if one's set, otherwise a hardcoded Python default baked into
`config.py` (e.g. `vertex_model: str = "gemini-2.5-flash"`). That baked-in
default is exactly the "potentially outdated default" risk: it only ever
gets exercised in the degraded case, so it can drift from reality (a
changed Vertex catalog, a deprecated model id) with nothing ever noticing.
Per your direction, the DB is the *only* source of truth here — a missing
value is a real, visible failure, not a silent default:

- `providers/active_model.py`'s cache widens from `dict[str, str]` (by
  provider) to `dict[tuple[str, int], str]` (by provider+slot). Its
  accessor becomes `active_model(provider, index) -> str`, and **raises**
  `LookupError` (or returns `None`, TBD by whoever implements — see the
  choice below) if `(provider, index)` has no row, rather than falling back
  to anything.
- New `providers/active_vertex_slot.py` (mirrors `active_model.py`/
  `key_index.py`'s existing cache-module shape): `active_vertex_project(index)`,
  `active_vertex_location(index)`, same no-fallback contract.
- `factory.py::_build`'s vertex branch already receives `index` — it calls
  these accessors and, on a missing value, raises the *same* `ValueError`
  shape it already raises for "no credential configured" (`f"no credential
  configured for provider={provider!r} index={index}..."` becomes a
  sibling `f"no model/project/location configured for provider={provider!r}
  slot={index}"`). This is not a new failure mechanism: `specialists/base.py`
  already has a broad `except Exception` around `get_provider()` that turns
  any `ValueError` here into a normal, visible per-specialist failure row —
  exactly this project's existing "partial failure always visible"
  convention, just reused for a new cause.
- `dispatcher.py::process_next_due` gains one more refresh call, same
  cadence and fail-safe shape as `_refresh_model_overrides` — "fail-safe"
  here means "never crash the dispatcher if the DB read itself fails,"
  which is different from "silently default when a row is simply absent."
  A DB read that succeeds but returns nothing for the active `(provider,
  index)` is not a refresh failure — it's a real missing-config state, and
  must surface as the `ValueError` above the next time that provider/slot
  is actually used, not be swallowed here.

## 5. Consequence: the DB must always be fully seeded

Removing the env-default fallback means normal operation now depends on
`slot_config` (and, per §6, the flat DB-only tuning knobs) always having a
real row for whatever `(provider, active slot)` is actually in use. Two
places currently don't guarantee that, and both need hardening:

- **`scripts/deploy.py --sync-config-db`**: today this mirrors
  `.env.config`'s *optional* tuning values into `runtime_config` — a
  best-effort sync, not a requirement (a value simply isn't pushed if
  unset locally). Once these fields have no fallback, this command must
  refuse to leave `slot_config`/the flat DB-only columns *unset* for
  whatever slot/provider is actually configured as active — either by
  requiring the relevant local values be present before syncing, or by
  failing loudly (mirroring `sync_env()`'s existing "refuse to push empty
  values" guard) rather than silently completing a partial sync.
- **`onboarding-wizard`'s provisioning flow**: today it pushes credential +
  model as Render env vars to a fresh service (`_LLM_ENV_VAR_NAMES`). It
  needs an equivalent DB-seeding step for a newly-provisioned instance's
  `runtime_config`/`slot_config` rows, run as part of the same provisioning
  transaction — a wizard-provisioned service must never boot into "no
  fallback, no DB row either" for its own just-configured provider.

This is real, load-bearing work, not a footnote — I'd want to treat "does a
freshly deployed/onboarded service have a fully-seeded config DB" as this
spec's actual acceptance test, more than any individual field's plumbing.

## 6. DB-only migration: 9 tuning knobs + Vertex project/location

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

`VERTEX_GCP_PROJECT`/`VERTEX_GCP_LOCATION` as flat (non-slot) settings are
superseded by §4's `slot_config` — there's no separate flat DB override for
them once slotting exists, only the per-slot values.

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

## 7. Dashboard implications

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

## 8. Guided-setup split-push hardening

This isn't a new problem this spec introduces — `_apply_llm_credential`
already writes to two backends in one apply (Render for the credential,
`runtime_config` for the model override), specifically so `active_model()`'s
DB-first read doesn't get shadowed by a stale value. §4/§5 make this
*larger*, not new in kind: model's Render write disappears entirely (no
more fallback to feed), and vertex's project/location gain a DB write
where today they have no DB path at all — one Render write (the
credential) plus up to three DB writes (model, and for vertex,
project/location) per apply.

- Extend the function's existing `{"applied": [...], "failed": [...]}`
  per-field reporting (already returned to `#renderSaveResult`, already
  rendered as the nested applied/failed list from the 2026-09-08
  prettify work) to cover every new field individually — a partial
  failure (e.g. credential pushed to Render, but the `slot_config` write
  failed) must show up as a named `failed` entry, not a silently
  incomplete apply.
- `payload.clear_vertex_gcp_project`'s current behavior (delete the Render
  env var) has no equivalent left once project is DB-only — "clearing"
  becomes writing `NULL` to the `slot_config` row's `vertex_gcp_project`
  column instead of an env-var delete.
- **Explicit test cases required** (not just "add tests" — these are the
  acceptance criteria): credential push to Render succeeds, `slot_config`
  write fails; credential push fails, `slot_config` write succeeds anyway
  (must it be prevented, or is a DB-only value with no matching live
  credential yet an acceptable transient state?); multiple `slot_config`
  fields in one apply where one field's write fails and another
  succeeds (must not partially commit a single logical row update as if
  it were independent field writes, unlike the Render-side keys which
  genuinely are independent single-key PUTs).

## 9. Credential deletion must nullify its slot's `slot_config` row

`config_deps.py::dependents_of`'s docstring currently says, correctly for
today: "model vars aren't credential-slot-specific, so deleting a credential
slot never needs to touch a model var." Slotting makes that false —
`slot_config` rows are now genuinely per-slot state that dangles the same
way `key_index_override`/`provider_override` already do when their slot's
credential disappears.

**Rule: deleting any credential slot var (`GEMINI_API_KEY[_n]`,
`GROQ_API_KEY[_n]`, or `VERTEX_GCP_SERVICE_ACCOUNT_KEY[_n]`) deletes that
`(family, index)`'s entire `slot_config` row** — for gemini/groq that's just
`model`; for vertex it's `model` *and* `vertex_gcp_project` *and*
`vertex_gcp_location` together, all three, never partially. Example: delete
`GROQ_API_KEY_1` → `slot_config` row `("groq", 1)` is gone, so a model
configured for that slot can never silently resurface — nothing is left to
be a "ghost" and misleadingly reused if a new credential later lands in
that same slot number. Same for `VERTEX_GCP_SERVICE_ACCOUNT_KEY_1` →
`slot_config` row `("vertex", 1)` (model + project + location) is gone.

**This is independent of whether the slot is currently active** — unlike
`dependents_of`'s existing `key_index_override`/`provider_override` checks,
which only fire for the *active* slot (deleting an unused spare is
currently a no-op dependents-wise). A spare, inactive slot can still carry
a leftover `slot_config` row from when it was last configured, and that's
exactly the ghost scenario being guarded against here — so this check must
run for *every* slot, active or not.

**Wire it through the existing confirm-before-delete flow, don't do it
silently**: `DeleteDependents` gains a new field (e.g. `slot_config: bool`,
set whenever `slot_index_for_var` resolves and that `(family, index)` has a
non-empty `slot_config` row), surfaced in `.labels()` and rendered in the
dashboard's existing "This will also clear:" confirmation dialog
(`deleteConfirmList`) alongside the key-index/provider-override lines —
this is real, meaningful config being discarded, the same category of
thing that dialog already exists to surface, not a silent side effect.

## 10. Open decisions for you to confirm or correct

1. ~~§4a: per-slot `model` for all three providers, or just Vertex
   project/location?~~ **Resolved: all three.**
2. ~~Is the `slot_config` table name/shape acceptable?~~ **Resolved as
   drafted**, with the persistence model in §4a made explicit.
3. ~~§7: per-slot dashboard UI in the same pass as the backend, or a
   follow-up?~~ **Resolved: best-effort in the same implementation pass**
   — the backend and UI work are expected to share the same or similar
   tests, so splitting them into separate passes would mostly duplicate
   effort rather than save any.
4. ~~Should the already-shipped 6 DB-only vars also lose their
   env-fallback tier?~~ **Resolved: yes.** `cooldown_config.py`,
   `usage_cap_config.py`, and `review_draft_config.py` all currently fall
   back to a `Settings` field default (e.g.
   `settings.dispatcher_rereview_cooldown_seconds`) when no DB override is
   set — that fallback branch is removed from all three, matching §4b's
   policy exactly: a missing value becomes a real failure, not a silent
   default. The `Settings` fields themselves don't disappear — per §5,
   they change *role* from "live runtime fallback" to "the seed value
   `--sync-config-db`/onboarding provisioning writes into the DB," nothing
   more.
5. ~~Raise directly, or return `None` and let the caller raise?~~
   **Resolved: return `None`, `_build` raises** — keeps the new cache
   modules exactly as narrow/dependency-free as `active_model.py`'s
   existing docstring describes.
