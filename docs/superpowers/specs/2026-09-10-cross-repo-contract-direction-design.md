# Cross-repo contract direction: bot-owned schema, wizard-owned provisioning

Supersedes the unlanded `2026-09-10-cross-repo-ref-bump-design.md`, which
proposed reciprocal pinned parity tests in both repos. That design was
discarded during review: its mutual exact-equality assertions could not be
satisfied by either repo independently (see §11).

## 1. Background: the direction was never stated, so it inverted

`pr-review-bot` and `onboarding-wizard` are developed together and never
share code. Everything crossing the boundary is duplicated by hand, by
convention. What the convention never wrote down is **which side owns
each duplicated fact** -- and that omission is what produced the
2026-09-09 incident.

`ISSUES.md:238-241` records it precisely:

> `onboarding-wizard`'s `router.py::_seed_provider_config` writes
> `runtime_config`'s singleton row (`id=1`, `provider`,
> `{provider}_key_index`) *before* the newly-provisioned service ever boots
> (required so `main.py`'s provider/slot_config boot check doesn't itself
> fail). `store.py::_seed_runtime_config_defaults` seeded the 9
> dispatcher/timeout tuning knobs ... with `INSERT ... ON CONFLICT (id) DO
> NOTHING` -- atomic against a concurrent seed, but its docstring's stated
> assumption ("seeding only ever fills a genuinely empty table") was
> silently false the moment a provisioning step created that row first.

18 of 22 columns NULL forever; every PR review on every wizard-provisioned
deployment stuck behind a "Dispatcher configuration issue" comment that
never resolved.

The mechanism was **row-creation order**, not the earlier migration of the
tuning knobs into the database. The wizard had to create the row first --
forced by the bot's own boot gate -- which made the wizard the *producer*
of that row while the bot's seeding code still assumed it was.

The 2026-09-09 fix removed bot-side seeding entirely and made the bot fail
loudly on an incomplete row (`store.py:210-213`, `main.py:129-138`). That
closed the incident but left the bot unable to tolerate any lag in its
consumer, and left the wizard hand-duplicating 22 column declarations plus
15 default values whose only guard is a pinned test in the *other* repo's
CI.

## 2. The two arrows, stated

Both directions are real. Naming them separately is the point of this
design:

- **Schema and defaults: bot -> wizard.** The bot declares
  `runtime_config`/`slot_config`'s shape and every operational default.
  `store.py:29-39` already says `RUNTIME_CONFIG_COLUMNS` is "the single
  source of truth for that table's shape."
- **Provisioning state: wizard -> bot.** At runtime the wizard creates the
  tables and writes the first row; the bot reads them.

The rule that falls out, and the rule this whole design serves:

> **The wizard writes only what it uniquely knows. The bot supplies
> everything it can derive itself.**

The wizard uniquely knows which provider a visitor chose, which slot, and
which model. It does not know -- and should not carry a copy of --
`dispatcher_failure_max_backoff_seconds`'s default.

## 3. Boot backfill: the bot fills what it owns

### 3.1 Column shape self-heals

`store.init_pool()` gains, immediately after its existing
`conn.execute(_SCHEMA)`:

```
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS <name> <type>;
```

one line per column in `RUNTIME_CONFIG_COLUMNS` missing from the live
table, reusing `deploy.py:585-591`'s existing generator and
`_missing_runtime_config_columns()`.

This is the mechanism `router.py:254-266` was working around. Read its
stated reason for demanding the wizard duplicate all 22 columns:

> `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already
> exists, so if the wizard created a narrower table here first, that
> project's own `store.init_pool()` would **never widen it later**

Once the bot widens, that requirement expires (§7).

`ADD COLUMN IF NOT EXISTS` is idempotent and declarative in exactly the
way `store.py:65-72` already carves out for `ENABLE ROW LEVEL SECURITY`
-- a no-op when satisfied, never an error, not a column-shape migration
in the "recreated out of band rather than migrated in place" sense that
section rejects. This is a deliberate, narrow extension of that carve-out,
not an abandonment of "declared, not migrated."

**`slot_config` gets the same treatment.** The hazard is identical and
currently unguarded: `store.get_slot_config()` SELECTs `model,
vertex_gcp_project, vertex_gcp_location` by name (`store.py:998`) and
`set_slot_config()` INSERTs them (`store.py:1027`), so a `slot_config`
the provisioner created narrower would raise `UndefinedColumn` from the
bot's own read path. `deploy.py:585-591`'s generator is
`runtime_config`-specific; it is generalized to take a table name and a
column tuple, and `init_pool()` runs it for both tables. Without this,
`runtime_config` self-heals and `slot_config` does not, for no stated
reason.

**Constraint: a column is ALTER-able only if it is nullable OR carries a
`DEFAULT`.** `ADD COLUMN ... NOT NULL` with no default fails against a
non-empty table. `deploy.py:591` emits the full declared type string, so
a missing `updated_at` would generate `ADD COLUMN IF NOT EXISTS
updated_at TEXT NOT NULL` and error. Unreachable today (`updated_at` is
always written by whoever creates the row), reachable once this runs at
boot. A test must assert every column in `RUNTIME_CONFIG_COLUMNS` is
nullable or defaulted, with `updated_at` and `id` documented as the
provisioner's responsibility instead (§6).

### 3.2 Values backfill without clobbering

One statement, after the ALTERs:

```sql
INSERT INTO runtime_config (id, c1, c2, ...) VALUES (1, %s, %s, ...)
ON CONFLICT (id) DO UPDATE SET
  c1 = COALESCE(runtime_config.c1, EXCLUDED.c1),
  c2 = COALESCE(runtime_config.c2, EXCLUDED.c2), ...
```

Defaults come from `Settings`' **declared** field defaults, resolved
through `deploy.py:1256-1283`'s existing column-to-setting mapping -- never
from the `settings` instance (see §5.3).

**This is not the seeding that 2026-09-09 removed.** The defect was a
row-existence assumption: `ON CONFLICT (id) DO NOTHING` asks "does row 1
exist?" and skips every column when it does. `DO UPDATE SET col =
COALESCE(...)` asks the right question per column -- it creates the row
when absent, fills only NULLs when present, and cannot overwrite a value
the wizard, `deploy.py`, or the dashboard wrote. `init_pool()`'s docstring
must lead with this distinction; without it the next reader will correctly
read this as a regression of an incident still logged in `ISSUES.md`.

A `SELECT` immediately before the upsert identifies which columns were
NULL, so the backfill can **log every column it filled at WARNING**
(names only). Without that, "it booted" stops proving the row was
deliberately provisioned, and an under-provisioned deployment becomes
indistinguishable from a healthy one in the logs.

### 3.3 A `None` default is a no-op, by design

`key_usage_token_cap`'s declared default is `None` (`config.py:176`), so
its clause reduces to `COALESCE(col, NULL)` -> `col`. Nothing to clobber,
no special-casing needed.

This is the correct behaviour, not an accident: `router.py:315-318` and
`usage_cap_config.py`'s docstring both record that a NULL cap paired with
a real reset time means "cap intentionally disabled" -- a valid configured
state. The rule generalizes and is self-maintaining: **a column whose
declared default is `None` is correctly left alone by the backfill.** Any
future `X | None = None` field behaves right with no new flag.

`key_usage_token_cap` therefore stays `int | None = Field(default=None,
gt=0)`, unchanged. Encoding "no cap" as `0` instead was considered and
rejected: it is already the live read-side behaviour
(`effective_caps()` normalizes non-positive to `None`, so
`dispatcher.py:461`'s guard already treats 0 as no-cap), so it buys no
semantics -- while legalizing `0` would demote `config.py:168-175`'s
`gt=0` from a real constraint to a comment, leaving `effective_caps()`'s
normalization as the sole guard against the sticky defer-everything-forever
value that constraint exists to make unrepresentable. There is also no
operator-facing gap: a blank `KEY_USAGE_TOKEN_CAP=` already falls back to
the default via `_blank_values_fall_back_to_defaults`.

### 3.4 Loud failure retained only where the bot has no answer

`main.py`'s three gates split by whether the bot could fill the value
itself:

- **`provider` (103-110) and `slot_config` (117-121): keep failing
  loudly.** The bot cannot invent which provider a visitor chose. These
  are the wizard's actual promise.
- **The 9 tuning knobs (134-138): backfilled *before* the gate runs.**
  The gate itself is unchanged -- `dispatcher_tuning_config.problems()`
  reports both "not set" and "out of range", and after §3.2 the "not set"
  branch simply becomes unreachable, because every one of the 9 has a
  declared default. So the bot stops refusing to boot over a column it
  could have filled, while still refusing to boot over a value that is
  present and invalid. §4.3 *extends* this gate to two more field groups;
  nothing about it is dropped.

## 4. Write-path validation parity

Backfill fills NULLs. It does nothing about a **non-NULL but invalid**
value, and `main.py` validates only the 9 tuning knobs -- so without this
section, both of §3's safety nets have the same hole in the middle.

### 4.1 Current coverage is inconsistent within one endpoint

| Field group | `deploy.py --sync-config-db` | dashboard `PATCH /api/environment/config` | client input |
|---|---|---|---|
| 9 tuning knobs | `dispatcher_tuning_config.problems()` | same shared `problems(merged)`, whole-group rejection | `min`/`step` present |
| cooldown trio | `factor >= 1.0, 0 < base <= cap` (`deploy.py:1259-1266`) | **none** | `step="any"`, no `min` |
| usage cap pair | via `Settings` (`gt=0`, `time` coercion) | **none** | no `min`; reset is free text |
| `provider`, `key_index` | -- | validated | -- |

`environment.py:842-847` states the rationale the untreated groups also
need: "this endpoint is the ONLY place a bad value can be caught before it
reaches the dispatcher."

Reachable consequences, all returning HTTP 200 with the field reported as
`applied`:

- `usage_cap_reset = "4pm"` (`dashboard.html:919` is `type="text"`,
  placeholder only, sent raw at line 2627) -> `effective_caps()` raises
  `ValueError` on `time.fromisoformat` -> `(None, None)` ->
  `dispatcher.py:461` skips the block. The usage cap **fails open**,
  silently and permanently. This is `ISSUES.md:240`'s documented fail-open,
  reachable from the UI rather than only from a NULL column.
- `usage_cap_tokens = 0` or negative (no `min` on `dashboard.html:934`,
  `Settings`' `gt=0` never runs) -> normalized to "no cap". Same silent
  fail-open.
- `cooldown_factor = 0.5` (no `min` on any of the three inputs) ->
  `effective_config()` discards the whole triple as `(None, None, None)`,
  which its own docstring says callers "must treat ... the same way (defer
  the ticket, don't guess)". **Fails closed** -- re-reviews stall. Opposite
  direction, equally silent.

### 4.2 The mechanism is a repeat, and that is the lesson

`store.set_cooldown_override` and `set_usage_cap_override` both document
that their *"only caller, `scripts/deploy.py::sync_config_db()`, always
writes the full triple/pair straight from `.env.config`'s resolved
`Settings` values -- there is no partial-field write to merge with a
current value for."*

That is false. `_apply_config_patch` is a second caller and *does* merge
partial fields against current values. The docstring's stated assumption
is precisely what justified having no validation, and a later feature
invalidated it silently -- structurally identical to `store.py:210-213`'s
*"docstring's stated assumption ... was silently false the moment ..."*.
Two instances, both in `store.py`, both costing a production incident or
a live silent-failure path. This is a demonstrated recurring failure mode
in this codebase, and §9 records it as such.

### 4.3 One shared predicate per field group

`dispatcher_tuning_config.problems()` is the pattern already done right --
one predicate table, consumed by `deploy.py`, the dashboard, and the boot
gate, so no writer can disagree with another about what is usable.
Replicate it:

Both take a `dict` and return a list of human-readable reasons, matching
`dispatcher_tuning_config.problems()`'s existing signature so all three
read identically at every call site:

- **`cooldown_config.problems(config) -> list[str]`** -- extract
  `effective_config()`'s existing discard predicate (`factor < 1.0 or
  base > cap or base < 0 or cap <= 0`; note base of exactly 0 is valid per
  its docstring) into a `problems()`-shaped function. `effective_config()`
  then calls it rather than inlining the comparison, so there is exactly
  one definition.
- **`usage_cap_config.problems(config) -> list[str]`** -- wraps
  `time.fromisoformat` on the reset string plus the `gt=0`-or-`None` cap
  bound. `effective_caps()` deliberately does **not** delegate to it, and
  this asymmetry with `cooldown_config` is intentional: the reader
  *normalizes* where the writer *rejects*. A non-positive cap reads back as
  `(None, reset)` -- cap disabled, reset time still honored -- whereas
  `problems()` rejects the pair outright, which is the right answer for a
  writer and the wrong one for a reader that must not take a dispatcher
  tick down over a row already in the database. Both modules document the
  split so a later reader does not "fix" the inconsistency.

Consumers, all three groups:

1. `deploy.py --sync-config-db` -- replaces its inline cooldown guard with
   `cooldown_config.problems()`; gains a usage-cap check it never had.
2. `dashboard/environment.py::_apply_config_patch` -- validates the
   cooldown trio and the usage cap pair as whole groups before writing,
   exactly as it already does for the 9 knobs, with the offending reasons
   named and nothing partially written.
3. `main.py`'s lifespan -- validates **all three** groups, not one. A bad
   value written before this change (or by any future writer) then surfaces
   as an immediate boot failure rather than surviving reboots invisibly.

The stale "only caller" docstrings in `store.py` are corrected to name both
callers and to state that validation is the caller's responsibility via
the shared predicate.

### 4.4 Out of scope

Client-side input attributes (`pattern` on the reset field, `min` on the
cooldown and token-cap fields) are deliberately **not** in this design.
Server-side validation is the correctness fix; the inputs are a UI
concern to be brainstormed separately, and per `CLAUDE.md` any
`dashboard/static/` change requires the `ui-visual-review` skill.

## 5. The contract artifact

### 5.1 What it covers, and why it is not the schema

`runtime_config`'s column shape stops needing cross-repo enforcement the
moment §3.1 makes it self-healing. What has **no** self-healing and **no**
existing guard is the env-var name and placement surface.

The bot partitions `OPERATIONAL_KEYS` across five sync destinations
(`_ALWAYS_SYNCED`, `_GENERIC_OPERATIONAL_ENV_ATTRS`,
`_DB_SYNCED_OPERATIONAL_KEYS`, `_NEVER_SYNCED_OPERATIONAL_KEYS`, plus
slot-zero seeding) and `test_deploy_script.py:1570-1595` asserts that
partition completely -- no key outside a destination, no destination entry
outside `OPERATIONAL_KEYS`, no overlaps. That test is excellent and
entirely **intra-repo**.

So: rename `GITHUB_TARGET_REPO` to `GITHUB_TRACKED_REPOS`, update
`OPERATIONAL_KEYS` and `_ALWAYS_SYNCED`, and the bot's suite stays green
because it is self-consistent. The wizard keeps pushing the old name; the
bot reads the new one, finds it unset, silently falls back to its default
-- on every provisioned deployment. Identically for a placement move in
either direction (DB-only to Render, or back).

The contract's job is to **publish the partition the bot already
maintains**, so the wizard's push-set can be checked against it.

### 5.2 Shape

`contracts/provisioning.json` in the bot (entries elided -- every
`OPERATIONAL_KEYS` member, every provider, and every backfilled column
appears in the real file):

```json
{
  "generated_by": "scripts.gen_contract -- do not edit by hand",
  "contract_version": 1,
  "env_vars": {
    "GITHUB_TARGET_REPO":  {"placement": "always_synced"},
    "KEY_USAGE_TOKEN_CAP": {"placement": "db_only"},
    "RENDER_SERVICE_NAME": {"placement": "never_synced"},
    "GEMINI_MODEL":        {"placement": "slot_zero_seed"}
  },
  "providers": {
    "gemini": {
      "credential_var": "GEMINI_API_KEY",
      "model_var": "GEMINI_MODEL",
      "key_index_column": "gemini_key_index"
    }
  },
  "runtime_config": {
    "provisioner_required": ["id", "provider", "updated_at"],
    "provisioner_required_one_of": ["gemini_key_index", "groq_key_index",
                                    "vertex_key_index"],
    "bot_backfilled": [
      {"column": "cooldown_base_seconds", "sql_type": "DOUBLE PRECISION",
       "default": 300.0}
    ],
    "no_default_by_design": ["key_usage_token_cap"]
  },
  "slot_config": {
    "provisioner_required": ["provider", "slot_index", "model",
                             "updated_at"],
    "optional": ["vertex_gcp_project", "vertex_gcp_location"]
  }
}
```

Sources, all module constants and class metadata: `OPERATIONAL_KEYS`, the
five sync groups, `registry.KEY_INDEX_COLUMNS`,
`RUNTIME_CONFIG_COLUMNS`, `deploy.py`'s column-to-setting mapping, and
`Settings.model_fields[...].default`.

`key_usage_reset_time_utc` is the one field whose logical type differs
from its wire type: `time` in `Settings`, TEXT in the column. The
generator serializes it with `.isoformat()`, mirroring
`deploy.py:1269`, so the contract always carries the 3-part
`"04:00:00"` form. That single conversion, in one repo, replaces the
wizard's hand-typed string literal.

### 5.3 Secret handling

The file is committed here and vendored into another repo, so this is a
hard rule, not a convention: **the contract carries env-var *names* and
non-secret operational defaults only, never a credential value.** The
generator follows `gen_docs.py`'s "THE ONE RULE" verbatim -- it imports
the `Settings` **class** and reads `model_fields[...].default`, never the
module-level `settings` instance, which carries this machine's real
`DATABASE_URL`, API keys, and service-account material. No
`always_synced` entry (all credentials and identity) carries a default at
all. `tests/test_gen_docs.py` already pins that discipline by parsing the
generator's own import statements; the contract generator gets the same
test.

### 5.4 Freshness

Generated by `scripts/gen_contract.py`, deterministic output (no
timestamps, no unordered iteration, no absolute paths), with a
`generated_by` marker. A step is added to CI's existing `docs` job --
same `git add` / `git diff --cached --exit-code` byte-compare that keeps
`guide/reference/` honest. That job needs no Postgres, matching
`gen_docs`' own constraint.

This is the bot's **only blocking** contract check, and it is purely
local: no sibling checkout, no pin, no network.

### 5.5 The wizard vendors and pins, atomically

The wizard commits a verbatim copy at `contracts/provisioning.json`
alongside its existing `.ci/pr-review-bot-ref`. These are two halves of
one fact -- *which bot contract are we built against* -- so
`scripts/update_bot_contract.py` in the wizard rewrites **both together
or neither**:

1. `git -C <bot> fetch origin`
2. Resolve `origin/main` (never local `HEAD`, never a feature-branch tip
   -- a squash-merged branch tip becomes an undangling-able pin)
3. `git -C <bot> show <sha>:contracts/provisioning.json` -- extraction by
   git object, so no worktree is created inside the sibling's `.git` and
   a dirty sibling tree is irrelevant
4. Run the wizard's parity tests against the extracted copy
5. Green: write both files, print old -> new. Red: write neither, print
   the failures

Ref-file format is pinned: 40 hex characters, optional trailing newline,
`#`-prefixed comment lines permitted so a deliberately-held pin can
record why. The bot **needs no pin at all** -- its only cross-repo check
is advisory (§6.2), and an advisory check wants the consumer's current
`main`, not a reproducible old snapshot.

## 6. The tests, and why each is satisfiable alone

### 6.1 Blocking

**Bot** (`tests/test_provisioning_contract.py`):

- Generated contract is byte-identical to the committed one (§5.4).
- `provisioner_required` equals exactly what `main.py`'s boot gate
  demands -- so the contract cannot claim the wizard owns something the
  bot actually backfills, or vice versa. `provisioner_required_one_of` is
  a separate key because exactly one of the three `*_key_index` columns is
  written (whichever provider the visitor chose); the other two are
  legitimately NULL, so a flat required-list would be wrong in both
  directions.
- Every column in `RUNTIME_CONFIG_COLUMNS` is nullable or carries a
  `DEFAULT`, except those in `provisioner_required` (§3.1).
- Every `db_only` key is absent from `render.yaml`.

**Wizard** (`tests/test_bot_contract_parity.py`):

- Vendored copy is byte-identical to the bot's at the pinned ref.
- The wizard's Render push-set **covers** the contract's provisioning
  responsibilities (superset assertion).
- The wizard pushes **nothing** in `db_only`.
- The wizard's write-set covers `provisioner_required` for both tables.
- Its `_KEY_INDEX_COLUMNS` and per-provider credential var names match
  the contract's `providers` block.

### 6.2 Advisory

One scheduled job in the bot checks out `TovTechOrg/onboarding-wizard`
at `main` (both repos are public, so `actions/checkout` needs no token --
worth a workflow comment, since this breaks the day either goes private),
compares its vendored copy against the bot's current generated contract,
and reports a lagging consumer. Never blocking: a bot push must not be gated on a landed wizard
commit.

### 6.3 Why there is no deadlock

Every blocking assertion is a **subset/superset** relation or an
intra-repo consistency check, and ownership is asymmetric: the producer
never blocks on the consumer. A bot contract change lands green on its own
(its checks are local). The wizard then runs
`update_bot_contract.py`, which advances pin and vendored copy together
and goes green in one commit. Neither repo can reach a state that only the
other repo's unlanded commit could fix.

CI skips must not read as passes: both repos' cross-repo tests assert
`pytest.fail` rather than `pytest.skip` when `CI` is set but the expected
file is absent.

## 7. What changes in the wizard

### 7.1 Duplicated constants

- `_RUNTIME_CONFIG_SCHEMA` shrinks from 22 hand-typed columns to the
  provisioner-required set. §3.1 removes the "must be the FULL column set"
  requirement that `router.py:254-266` documents, along with its reason.
- `_RUNTIME_CONFIG_DEFAULTS` (15 hand-copied values) is **deleted**. With
  it goes the `"04:00:00"` string literal -- so the reset-time
  logical/wire type mismatch is removed rather than fixed.
- `_LLM_ENV_VAR_NAMES`, `_KEY_INDEX_COLUMNS` and
  `_GENERIC_OPERATIONAL_ENV_DEFAULTS` stay hand-written but become
  **asserted against the vendored contract**, which is what catches a
  rename. (Their comments also need correcting: they cite
  `_GENERIC_OPERATIONAL_ENV_ATTRS` as their source, which is now empty --
  `GITHUB_TARGET_REPO` actually lives in `_ALWAYS_SYNCED`. The behaviour
  is correct; only the provenance claim is stale.)
- `_SLOT_CONFIG_SCHEMA` is **unchanged**. Now that the bot widens this
  table too (§3.1), the wizard *could* shrink it to
  `slot_config.provisioner_required`, but there is nothing to gain: all
  six columns are either required or in `optional`, and the wizard writes
  five of them itself. Left full deliberately, not by omission.

### 7.2 The existing cross-repo integration

`tests/test_cross_repo_config_ordering.py`, `.ci/pr-review-bot-ref` and
the wizard's CI block already implement a version of this. Disposition,
piece by piece:

| Piece | Disposition |
|---|---|
| `test_runtime_config_column_parity` | **Retired.** Asserts ordered equality between the wizard's DDL and the bot's full 22-column tuple. §7.1 makes the wizard's DDL deliberately narrower and §3.1 makes narrowness harmless, so this assertion becomes false *by design*. Replaced by §6.1's superset check. |
| `test_slot_config_column_parity` | **Retired**, same reasoning; replaced by §6.1's coverage check against `slot_config.provisioner_required` plus `optional`. |
| `test_wizard_seed_leaves_bot_boot_ready` | **Kept, and it gets stronger.** It is the only test exercising the real chronology against live Postgres, and after §7.1 it stops proving "the wizard seeded 15 values correctly" and starts proving "the bot's ALTER + backfill makes a minimally-provisioned database boot-ready" -- the direct regression test for §3. Two required changes: its docstring must be rewritten to claim the new thing, and it must additionally assert the tuning columns came back **non-NULL**, or it cannot distinguish a working backfill from a wizard that seeded them anyway. |
| `_parse_ddl_columns`, `_CONSTRAINT_KEYWORDS`, `_bot_runtime_config_columns` | **Retired.** The DDL regex and the AST literal-eval exist only to compare source text across repos; a vendored JSON contract makes that `json.load` plus set operations. A much smaller column-*name* extractor survives for the wizard's own `_RUNTIME_CONFIG_SCHEMA` -- reading only its own source, never the bot's, so no cross-repo type-string normalization is needed. |
| `_pr_review_bot_path`, `_skip_if_bot_checkout_missing` | **Kept, but narrowed to tier-2 only.** The new parity tests read the vendored contract from the wizard's own tree, so they need no sibling checkout and **never skip**. Today all three tests skip without a sibling present; afterwards only the tier-2 chronology test does. |
| `.ci/pr-review-bot-ref` | **Kept**, with §5.5's pinned format, and rewritten atomically with the vendored contract by `update_bot_contract.py`. It now serves two purposes: tier-2's bot checkout, and the vendored-copy freshness comparison. |
| Wizard CI block (pinned checkout + `PR_REVIEW_BOT_PATH`) | **Kept, essentially unchanged**, plus §6.3's fail-don't-skip guard. |
| `uv sync --all-extras --dev` in the bot's directory | **Kept, and load-bearing** -- tier-2 runs `uv run --directory <bot>`, so the bot's dependencies must be installed. (This is *not* symmetric: the bot's own CI needs no `uv sync` in the wizard's directory, because §6.1's bot-side checks are static and §8 declines a bot-side tier-2. Installing the wizard's dependencies there would let a broken lockfile at the pinned commit redden the bot's CI for a reason unrelated to the contract.) |
| File name | **Kept.** With the two parity tests moved out, the file holds only the seed-then-boot chronology test -- which is what "config ordering" referred to all along. Its module docstring, which describes "two tiers" and three tests, needs rewriting. |

## 8. Non-goals

- **No shared or imported code.** The contract is a generated data file,
  vendored as a copy. No module crosses the boundary.
- **No tier-2 reciprocal test in the bot.**
  `test_wizard_seed_leaves_bot_boot_ready` covers the live-Postgres
  chronology from the wizard's side, and §6.1's assertions cover the
  bot's side statically. (The superseded design claimed this non-goal
  while its own §1 argued the opposite; here the static checks genuinely
  do run in the bot's own CI.)
- **No client-side input validation** (§4.4).
- **No automatic committing** of a bumped pin or vendored contract. The
  script rewrites files on disk; staging and committing stay explicit,
  per `CLAUDE.md`'s "never commit on someone else's behalf."

## 9. `CLAUDE.md` changes

**Bot**, under the existing *Module boundaries and contracts*, and
**wizard**, as a new top-level section (its duplication rules currently
sit inside "What sub-project 6 ... adds to these rules"):

> **The bot owns the schema contract; the wizard owns the row.** The bot
> declares `runtime_config`/`slot_config`'s shape and every operational
> default, backfills any column it can derive, and widens the table
> itself at boot. Whoever provisions the database writes only what it
> uniquely knows -- provider, key slot, model -- and the bot refuses to
> start if *that* is missing. These two arrows point opposite ways on
> purpose. The 2026-09-09 incident is what it looks like when one side
> silently takes over the other's end: the wizard had to create the row
> first, which made it the row's producer, while the bot's seeding code
> still assumed it was.

Plus, in both, a note on the recurring failure mode from §4.2:

> **A store-layer docstring that asserts a caller-set invariant ("the
> only caller always writes the full pair") is a validation gap waiting
> for its second caller.** This has now cost two incidents in
> `store.py`. Validate in a shared predicate the writers all call, not in
> prose about who calls you.

## 10. Rollout

1. **Bot, self-contained:** §3 backfill + ALTER, §4.3 shared predicates
   and the extended boot gate, and the corrected `store.py` docstrings.
   Nothing cross-repo; independently green.
2. **Bot:** `scripts/gen_contract.py`, `contracts/provisioning.json`, the
   `docs`-job freshness step, and §6.1's bot-side tests.
3. **Wizard:** vendor the contract, add `update_bot_contract.py`, add
   §6.1's wizard-side tests, then collapse §7.1's duplication and retire
   §7.2's superseded pieces -- in that order, so the replacement tests
   exist and pass before the constants they guard shrink and the old
   tests come out. The tier-2 chronology test is updated in the same
   step, since §7.1's shrink is what changes what it proves.
4. **Bot:** §6.2's advisory job.
5. Both `CLAUDE.md` sections (§9), and delete the superseded spec.

**Plan boundary.** Stage 1 is independently valuable and shippable --
it fixes the live silent-failure paths in §4.1 and makes the schema
self-healing, with nothing cross-repo. Stages 2-5 are the contract work
proper. These are best executed as two implementation plans rather than
one; stage 1 does not depend on any decision in stages 2-5.

Validation of the failure path, not just the plumbing: on a throwaway
branch, add a column to `RUNTIME_CONFIG_COLUMNS` and confirm (a) the bot
stays green and self-heals a pre-provisioned database, (b) the freshness
gate goes red until regenerated, (c) the advisory job reports the wizard
as lagging. Then discard the branch. The superseded design's rollout
proved only that the plumbing ran on an unchanged schema.

**Validated 2026-09-10.** Run on a throwaway branch that added
`dispatcher_probe_interval_seconds DOUBLE PRECISION` (declared default
`11.0`) to `RUNTIME_CONFIG_COLUMNS`, with the four hand-maintained lists a
new column must reach: `config.py`'s `Settings`,
`store.RUNTIME_CONFIG_COLUMNS`, `runtime_config_defaults.COLUMN_TO_SETTING`,
and `deploy._DB_SYNCED_COLUMNS`, plus a fifth Step 3 did not name:
`tests/test_store_schema.py::EXPECTED_COLUMNS["runtime_config"]`, a
hardcoded lock-down of the *live* Postgres schema that guards against
unintended drift from the "declared, not migrated" refactor -- distinct
in kind from the other four because it isn't part of the published
contract, but a column that widens the live table via `ALTER` trips it
too.

- **(a) The bot stays green and self-heals a pre-provisioned database.**
  `uv run pytest -v` and `ruff check .` passed (1429 passed, clean) once
  the fifth touchpoint above was fixed -- the first run failed exactly
  there (1 failed, 1428 passed) and nowhere else, confirming the five-list
  count rather than a coincidental break elsewhere. The proof that matters
  is cross-repo: with the new contract vendored into the wizard,
  `test_wizard_seed_leaves_bot_boot_ready` passed -- the wizard provisions
  a six-column table that has never heard of the new column, and after the
  bot's own `init_pool()` the column reads back non-NULL at its declared
  default, with `dispatcher_tuning_config.problems()` empty. (The as-is
  run, with the wizard's vendored contract left unchanged, also passed but
  does not cover the new column -- it only confirms the known columns
  still work with a wider bot schema in play. Both runs were performed;
  only the second is evidence for the new column.)
- **(b) The freshness gate goes red until regenerated.** CI's two `docs`-job
  steps exited 1 against the stale committed contract -- the printed diff
  added a `bot_backfilled` entry naming `dispatcher_probe_interval_seconds`,
  `sql_type: "DOUBLE PRECISION"`, `default: 11.0` -- and exited 0 after
  `gen_contract` was re-run and the change committed.
- **(c) The advisory job reports the consumer as lagging.**
  `check_consumer_contract` returned `LAGGING`/exit 1, naming
  `runtime_config.bot_backfilled[dispatcher_probe_interval_seconds]`'s
  `.default` and `.sql_type` as missing from the consumer's copy. The
  consumer's pin was unchanged throughout (`3fee149...`), confirming the
  verdict is driven by the vendored contract and not by pin age.

The branch was discarded; both repositories returned to `IN_SYNC` with clean
trees and byte-identical regenerated artifacts.

(§10 step 5's "delete the superseded spec" was a no-op: the reciprocal-pin
design was never committed to either repository. §1 and §11 keep the record
of why it was discarded.)

## 11. Why the reciprocal-pin design was discarded

Both repos asserting **exact set equality** against a pinned sibling
cannot be satisfied. A bot PR adding a column is red until a wizard commit
carries it; the wizard PR carrying it is red until a bot commit has it.
Neither can be green first, and expand/contract does not help -- an
equality assertion has no backward-compatible intermediate state by
construction. That design also gave its local pre-push script an override
(`SKIP_SIBLING_PIN_CHECK=1`) while CI had none, so the override silenced
the script and left CI red, in a repo whose convention is never to push
with a red suite.

Asymmetric ownership dissolves the problem rather than working around it:
the producer's checks are local and always satisfiable, and the consumer's
are subset-shaped and fixable in one commit.
