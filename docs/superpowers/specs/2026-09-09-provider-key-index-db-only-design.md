# Provider/key-index become DB-only (no `LLM_PROVIDER` env fallback)

## 1. Background

The 2026-09-08 slotted-config work
(`docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md`)
made each provider's *model* (and Vertex's project/location) genuinely
DB-only, per credential slot, with no env fallback — but deliberately left
*provider selection* and *key-index selection* out of scope: `LLM_PROVIDER`
(env) remains the primary source, with `runtime_config.provider` /
`runtime_config.{provider}_key_index` layered on top as an optional live
override (`providers/active.py` / `providers/key_index.py`).

Inspecting a real onboarding-wizard-provisioned database
(`onboarding-wizard`'s sibling project) surfaced that this two-tier design
is effectively inert in practice: `runtime_config.provider` and every
`*_key_index` column were `NULL`, meaning the deployment's actual behavior
was governed entirely by Render's `LLM_PROVIDER` env var and the
hardcoded index-0 default — `runtime_config` carried none of the facts
that were supposedly true "sources of truth" for it. This is the same kind
of drift the 2026-09-08 work already eliminated for `model`; this spec
generalizes that fix to `provider` and `key_index`.

This project is not in production. Every deployment (service + every
external dependency) is recreated from scratch via the onboarding wizard,
so this spec carries **no migration path** for already-running instances —
a hard cutover is acceptable and is the design chosen below.

## 2. Goal

1. `runtime_config.provider` becomes the sole source of truth for which
   provider is active — no `LLM_PROVIDER` env var anywhere, in either repo.
2. Boot refuses to start unless `runtime_config.provider` is set AND a
   `slot_config` row exists for `(provider, active_key_index(provider))` —
   whether that index came from an explicit override or the implicit
   index-0 default. A missing model for the active slot is exactly as fatal
   as a missing provider.
3. The onboarding wizard seeds `runtime_config.provider` and the matching
   `..._key_index` (always `0`, since the wizard only ever provisions the
   base credential slot) in the same step it already seeds `slot_config`,
   so a freshly-provisioned deployment is never in the "boot refuses to
   start" state the new check above introduces.

## 3. Explicitly out of scope

- `slot_config`'s `vertex_gcp_project`/`vertex_gcp_location` columns stay
  as flat, nullable columns — considered and rejected as unnecessary
  churn for a two-column, low-row-count table (see section 6).
- Any migration/back-compat path for existing deployments — none exist to
  migrate.
- A general redesign of `providers/key_index.py`'s implicit-index-0-default
  mechanism — that stays exactly as it is. What changes is that boot now
  *verifies* a `slot_config` row exists for whichever index ends up active,
  closing the gap where a valid-looking index pointed at nothing.

## 4. pr-review-bot changes

- **`config.py`**: remove the `llm_provider: str = ""` field entirely.
  `LLM_PROVIDER` is no longer read anywhere in this codebase.
- **`providers/active.py`**: `active_provider()` stops falling back to
  `settings.llm_provider` — it becomes a pure read of the refresh-cache
  (`_override`), same shape as `providers/active_model.py`. Calling it
  before the cache has ever been populated, or when the DB row is `NULL`,
  is a real "unconfigured" condition; the caller (ultimately `main.py`'s
  lifespan, see below) is responsible for turning that into a boot failure
  rather than this module inventing a silent default.
- **`main.py`'s `lifespan`**: replace the current
  `if settings.llm_provider not in registry.PROVIDERS: raise RuntimeError(...)`
  check with a DB-backed check: refuse to start unless
  `runtime_config.provider` is set to a name in `registry.PROVIDERS`, *and*
  a `slot_config` row exists for `(provider, active_key_index(provider))`.
  Same fail-loudly philosophy as today's check, just pointed at the DB.
  This subsumes today's "is the provider name recognized" check and adds
  the "does its active slot actually have a model configured" check that
  didn't exist before.
- **`scripts/deploy.py`**: every place that currently treats
  `settings.llm_provider` as a legitimate value loses its "env" branch —
  there is no more env value to read, fall back to, or compare an override
  against. Concretely (not exhaustive — the implementation plan enumerates
  exact edits): `_resolved_provider`, `_resolved_provider_or_env`,
  `check_provider`, `check_provider_live`, and `check_config`'s "LLM_PROVIDER
  is unset" problem all collapse to "read `runtime_config.provider`
  (required, no fallback), full stop" — the same DB read these functions
  already do for the override case, just no longer optional. Anywhere that
  reported `source = "env"` or `f"DB override; env={settings.llm_provider}"`
  now just reports the DB value directly, since there is only one source
  left.
- **`scripts/doctor.py`**'s `check_llm_provider` (today: "is `LLM_PROVIDER`
  set locally, so an operator without `DATABASE_URL` yet isn't stuck") has
  nothing left to check locally once provider lives only in the DB. Fold
  its remaining purpose (credential-presence check for whichever provider
  is active) into `deploy.check_provider`, accepting that check now SKIPs
  without `DATABASE_URL` (same as it already does) rather than keeping a
  separate local-only check with no local value to inspect.
- **`scripts/demo_provider_swap.py`**: delete. It exists specifically to
  demonstrate `LLM_PROVIDER` as a live-swappable runtime seam by
  monkeypatching `settings.llm_provider` at runtime; that seam no longer
  exists once provider is DB-only, and no replacement demo is in scope.
- **`render.yaml`**: remove the `LLM_PROVIDER` env var entry (currently
  line 38).

## 5. onboarding-wizard changes

- **`router.py`'s `bulk_push_render_env_vars`**: drop `env_vars["LLM_PROVIDER"] = ...`
  (currently line 805) — Render never receives this var.
- **`_seed_slot_config`** (or a sibling step run in the same connection,
  right alongside it): also write `runtime_config.provider = <chosen
  provider>` and `runtime_config.{provider}_key_index = 0` (the wizard only
  ever provisions slot 0). Same transaction/connection as the existing
  `slot_config` seed, same ordering guarantee already documented for it: a
  seed failure refuses the whole call (`{"valid": false, "reason":
  "slot_config_seed_failed"}` or a sibling reason code) before
  `render_client.push_env_vars` is ever called — the visitor must never
  reach a state where Render is missing `LLM_PROVIDER` (already true, it's
  never pushed) while the DB is also missing the provider/index that make
  the deployment bootable.
- **`runtime_config` needs a row to `UPDATE`/`UPSERT` into** — same
  `INSERT ... ON CONFLICT (id) DO UPDATE` shape `_seed_slot_config` already
  uses for its own table, applied to `runtime_config`'s singleton
  `id = 1` row instead.

## 6. `slot_config`'s per-provider columns (considered, rejected)

Three options were considered for `vertex_gcp_project`/`vertex_gcp_location`
being `NULL` for every non-Vertex row:

1. **Leave as-is** — two nullable `TEXT` columns on a small, low-row-count
   table; every reader already treats them as Vertex-only by convention in
   code/comments.
2. A companion `slot_config_vertex` table, 1:1 with `slot_config`, populated
   only for `provider='vertex'` — removes the `NULL`s but adds a join to
   every read path (`get_slot_config`, `get_all_slot_configs`, the
   dispatcher's refresh query) that today is a single-table `SELECT`.
3. A `JSONB extra` column replacing both, namespaced per provider — removes
   the `NULL`s and gives future provider-specific fields a home with no
   further migrations, at the cost of losing column-level typing.

**Decision: option 1.** Given the real table has a single-digit row count
and the columns already read as intentional (not accidental) everywhere
they're touched, this is left unchanged.

## 7. Testing

- pr-review-bot: update/replace every test that currently sets
  `settings.llm_provider` as a fixture value (main.py lifespan tests,
  `providers/active.py` tests, `scripts/deploy.py` tests) to seed
  `runtime_config.provider`/`{provider}_key_index` instead. Any test
  asserting the old "env is the fallback" behavior is replaced with a test
  asserting the new "DB value required, boot fails without it" behavior.
  Delete `demo_provider_swap.py`'s own invocation from any script/CI that
  runs it, if one exists.
- onboarding-wizard: extend the existing `_seed_slot_config` test coverage
  (`tests/test_onboarding_router.py`) to also assert the
  `runtime_config.provider`/`{provider}_key_index` row is written
  correctly, and that a seed failure still refuses the Render push (mirrors
  the existing `slot_config`-only test).

## 8. Non-goals / unaffected

- Credentials (`GEMINI_API_KEY`, `GROQ_API_KEY`,
  `VERTEX_GCP_SERVICE_ACCOUNT_KEY`[+slots]) remain env-var-only, never
  DB-stored — unchanged from the 2026-09-08 spec's own scoping.
- The dashboard's existing "flip active provider/key without a redeploy"
  feature (`dashboard/environment.py`, `store.set_provider_override` /
  `set_key_index_override`) is unaffected in shape — it already writes to
  `runtime_config`, which now is simply the *only* place that value can
  come from instead of one of two.
