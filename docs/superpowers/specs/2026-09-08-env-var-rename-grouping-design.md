# Env-var rename for alphabetical-sort grouping

## 1. Goal

Now that the dashboard's Environment tab sorts env vars and model dropdowns
alphabetically (see `2026-09-02-dashboard-environment-tab-design.md` and the
2026-09-08 sort/prettify work that followed it), a handful of env var names
don't group with their own provider/subsystem family the way alphabetical
sort assumes they will. This spec renames exactly those, in both
`pr-review-bot` and its sibling `onboarding-wizard` repo, so each family
sorts together.

## 2. Exact rename set

| Old name | New name | Why |
|---|---|---|
| `GCP_PROJECT` | `VERTEX_GCP_PROJECT` | Vertex-specific, wasn't grouped with `VERTEX_MODEL`/`VERTEX_GCP_SERVICE_ACCOUNT_KEY`. |
| `GCP_LOCATION` | `VERTEX_GCP_LOCATION` | Same. |
| `GCP_SERVICE_ACCOUNT_KEY` (+ numbered slots `_1`..`_4`) | `VERTEX_GCP_SERVICE_ACCOUNT_KEY` (+ `_1`..`_4`) | Same; slot-suffix convention (`config_deps.py::slot_env_name`) is unchanged, just applied to the new base name. |
| `LLM_MODEL` | `GEMINI_MODEL` | Actually Gemini-only (see `config.py`'s own comment on the field — kept its pre-multi-provider generic name), unlike the correctly-prefixed `GROQ_MODEL`/`VERTEX_MODEL`. |
| `DEFAULT_RETRY_AFTER_SECONDS` | `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS` | Dispatcher-family fallback (used when a provider's rate-limit response carries no `Retry-After`), but was the only one of its 8 siblings without the shared `DISPATCHER_` prefix. |

`VERTEX_MODEL`, `GROQ_API_KEY`, `GROQ_MODEL`, `GEMINI_API_KEY` are already
correctly grouped and are untouched.

## 3. Explicitly out of scope

- **`.env` / `.env.config` themselves** — the user edits these by hand;
  nothing here writes to them.
- **`docs/superpowers/plans/**` and `docs/superpowers/specs/**`** (both
  repos) — dated planning artifacts describing decisions made under the
  names live at the time. Left untouched, same as this project's existing
  convention of never rewriting `ISSUES.md`'s historical entries (e.g. the
  already-renamed `GCP_SERVICE_ACCOUNT_KEY_B64`).
- **`ISSUES.md`'s historical incident entries** — e.g. the entry
  referencing `LLM_MODEL` while describing what was checked during a past
  incident stays exactly as written; it's a record of what was actually set
  at that time, not a place documenting current config shape.
- **Inline historical narration inside otherwise-current docs** — `SPEC.md`
  and `cost.md` are living documents and DO get their current-state
  references renamed, but a few sentences in `cost.md` §4 narrate a
  specific dated verification ("live and fully verified as of 2026-08-14
  ... Confirmed live with `LLM_MODEL=gemini-2.5-flash`") — those specific
  clauses keep the old name they actually used at the time, for the same
  reason `ISSUES.md` entries do. Everything else in both files (schema
  descriptions, current defaults, current examples) is renamed.
- **`runtime_config` Postgres schema** (`review_queue/store.py`'s
  `RUNTIME_CONFIG_COLUMNS` — `cooldown_base_seconds`, `key_usage_token_cap`,
  `gemini_model`, `vertex_key_index`, etc.) — column names are unchanged.
  What matters instead is that the resolution path from a DB-stored
  override (e.g. `vertex_key_index`/`vertex_model`) to the actual env var it
  reads stays correct after the rename — see §5.
- **Any live Render API call** (delete/recreate a key on the deployed
  service) — the user is deleting and redeploying both the bot and wizard
  Render services fresh, so there is nothing live to migrate. This spec is
  a code/docs rename only.

## 4. Cascade mechanics

None of these are pure string substitution. Each old name is bound through
several layers that must change together:

- **`pr-review-bot/config.py`**: the `Settings` field itself (`gcp_project`,
  `gcp_location`, `gcp_service_account_key`, `llm_model`,
  `default_retry_after_seconds`) — pydantic-settings binds a field to an env
  var by case-insensitive name match today, no explicit `alias=`, so the
  field name must become e.g. `vertex_gcp_project` for
  `VERTEX_GCP_PROJECT` to bind. Every `settings.<old_field>` reference
  anywhere in the codebase moves with it.
- **`providers/registry.py`**: `PROVIDERS = {"vertex": ("GCP_SERVICE_ACCOUNT_KEY",
  "VERTEX_MODEL"), "gemini": ("GEMINI_API_KEY", "LLM_MODEL"), ...}` — the
  single source of truth both `config_deps.py`'s slot-name derivation
  (`slot_env_name`/`credential_slot_vars`) and the dashboard's credential
  resolution (`dashboard/environment.py::_resolve_current_credential`,
  `_fetch_models_for_provider`) read from. Updating these tuples is what
  keeps a DB-stored `vertex_key_index`/`gemini_model` override resolving
  against the *new* env var names automatically — this is the crux of the
  correctness requirement raised in clarification, not a separate mechanism.
- **`scripts/deploy.py`**: `_PROVIDERS`, `_wanted_env()`,
  `_GENERIC_OPERATIONAL_ENV_ATTRS` (for `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`),
  and every `_unpriced_models()`/sync-guard reference.
- **`dashboard/environment.py` + `dashboard/static/dashboard.html`**: the
  `ENV_VAR_DESCRIPTIONS` dict keys (both `en`/`he`), the `CONFIG_FIELD_ENV_VAR`
  mapping added in the 2026-09-08 config-panel info-hover work, and
  `CREDENTIAL_SLOT_BASE_VARS`.
- **`onboarding-wizard/router.py`**: `_LLM_ENV_VAR_NAMES` and
  `_GENERIC_OPERATIONAL_ENV_DEFAULTS` — the latter has a deliberate comment
  explaining why `GCP_PROJECT` is excluded from the pushed defaults; that
  exclusion (and its comment) moves to `VERTEX_GCP_PROJECT` unchanged in
  meaning.

## 5. Verification requirement

Because the rename touches the credential/model resolution path, not just
display strings, the following must be exercised by tests (existing or
added) after the rename, per repo:

- A DB-stored `vertex_key_index` override still resolves to the correct
  `VERTEX_GCP_SERVICE_ACCOUNT_KEY[_n]` slot value.
- A DB-stored `vertex_model`/`gemini_model` override still resolves to the
  correct active model for that provider (`providers/active_model.py`).
- The dashboard's guided-setup Vertex flow and its model-catalog fetch
  (`_fetch_models_for_provider`) still work end-to-end against the renamed
  vars.
- `scripts/deploy.py --sync-env`'s `_wanted_env()` still emits the new
  names and no longer emits any old name.

## 6. Files touched (current-state only)

### `pr-review-bot`

Core code: `config.py`, `config_deps.py`, `providers/registry.py`,
`providers/vertex_credentials.py`, `providers/active_model.py`,
`providers/base.py`, `providers/google_genai.py`, `providers/groq.py`,
`providers/factory.py`, `render_client.py`, `scripts/deploy.py`,
`scripts/encode_credential.py`, `scripts/set_override.py`,
`scripts/init_env.py`, `scripts/manual_verify_vertex.py`,
`dashboard/environment.py`, `dashboard/static/dashboard.html`.

Tests: `tests/test_config.py`, `tests/test_config_deps.py`,
`tests/test_provider_registry.py`, `tests/test_vertex_credentials.py`,
`tests/test_render_client.py`, `tests/test_deploy_script.py`,
`tests/test_active_model.py`, `tests/test_providers.py`,
`dashboard/tests/test_dashboard_page.py`, `dashboard/tests/test_environment.py`.

Config templates / deploy: `render.yaml`, `.env.example`,
`.env.config.example`.

Docs: `CLAUDE.md`, `SPEC.md`, `cost.md` (except the dated passage in §3),
`guide/reference/sync-env.md`, `guide/reference/config.md`,
`guide/setup/04-llm-provider.md`, `guide/operations/overrides.md`,
`guide/background/providers.md`.

### `onboarding-wizard` (separate git repo)

`router.py`, `tests/test_onboarding_router.py`, `CLAUDE.md`.

## 7. Execution shape

Two independent rename passes, one per repo (separate commits/PRs — the
repos share no git history). Each pass:

1. Rename in code + tests together (a field/tuple rename and its test
   updates are one unit, not a rename-then-fix-tests sequence).
2. Run that repo's full test suite + lint; both must be green with zero
   remaining references to any old name outside the excluded files in §3.
3. `grep` sweep across each repo (excluding the §3 exclusions) confirming
   no old name survives, before considering that repo's pass done.

No live deploy/migration step is needed from either pass — the user
redeploys both services fresh with hand-edited env vars afterward.

## 8. Addendum (post-implementation review, 2026-09-08)

Two corrections to this spec's own text, recorded rather than silently
edited, found by a code-correction review that ran after the same-day
follow-up spec (`2026-09-08-slotted-config-and-db-delegation-design.md`)
had also landed:

- §4's fourth bullet places `ENV_VAR_DESCRIPTIONS`, `CONFIG_FIELD_ENV_VAR`,
  and `CREDENTIAL_SLOT_BASE_VARS` in `dashboard/environment.py`. They
  actually live in `dashboard/static/dashboard.html` (`ENV_VAR_DESCRIPTIONS`
  and `CREDENTIAL_SLOT_BASE_VARS` as JS constants, with the field
  descriptions rendered from `cfg_desc_*` i18n keys). The rename itself was
  applied in the right place; only this file list was wrong.
- §5's second verification bullet ("A DB-stored `vertex_model`/
  `gemini_model` override still resolves to the correct active model") was
  superseded within the same day by the slotted-config-and-db-delegation
  spec's §4b, which replaced the flat `runtime_config.{provider}_model`
  path with per-slot `slot_config`. The equivalent requirement is now "a
  `slot_config` row for `(provider, active slot)` resolves to the correct
  active model" — see that spec's §11 addendum for how the flat columns
  were retired. Any test written against `store.get_model_override` (which
  no longer exists) was satisfying the letter of this bullet while
  exercising a column nothing reads.
