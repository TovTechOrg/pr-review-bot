# Cross-repo contract Stage 5: conventions, supersession cleanup, and end-to-end validation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the cross-repo contract rollout — prove the whole mechanism works against a *real* contract change (not just an unchanged schema), then write the ownership direction into both repos' `CLAUDE.md` and correct the prose Stages 1–3 silently falsified.

**Architecture:** One validation task first, four documentation tasks after. The ordering is deliberate: §9's `CLAUDE.md` text asserts as settled fact that "the bot backfills any column it can derive and widens the table itself at boot." Task 1 is what earns the right to write that sentence — per `CLAUDE.md`'s own rule that documentation describing a live-verification outcome is written *after* the step runs, not drafted in advance assuming success. Tasks 2–5 then land the prose, in the bot and in the wizard, each with its own commit in its own repo.

**Tech Stack:** Markdown, Python 3.12, pytest (`-n 4`, real Postgres available locally), ruff, git. No new dependencies, no new code modules.

**Spec:** `docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md` — this plan implements **Stage 5 only** (§10 rollout step 5: "Both `CLAUDE.md` sections (§9), and delete the superseded spec") **plus §10's closing validation paragraph**, which belongs here because it is the last stage and it needs every earlier stage landed. Read §2, §3, §4.2, §5.4, §6.2, §7.1, §9 and §10 before starting.

---

## Where the rollout actually stands (verified 2026-09-10, before this plan was written)

Do not re-derive this; it was checked directly. Do re-check anything a task's own step tells you to.

| Stage | State |
|---|---|
| 1 — bot backfill/ALTER, shared predicates, extended boot gate | **Landed** on `pr-review-bot` `main` (`fbc6cd1`…`1e1db06`). |
| 2 — `gen_contract.py`, `contracts/provisioning.json`, `docs`-job freshness step, bot-side tests | **Landed** (`542bfba`…`3fee149`). |
| 3 — wizard vendors the contract, `update_bot_contract.py`, parity tests, duplication collapse | **Landed** on `onboarding-wizard` `main` (`abe7924`…`76be47e`). |
| 4 — advisory consumer-lag job | **Landed** (`35839c3`…`fd9b0cb`). |
| 5 — this plan | Not started. |

Both repos are on `main` with clean working trees, and the live advisory check currently reports `IN_SYNC` (consumer pinned at `3fee149`, five commits behind `main`, contract byte-identical). That `IN_SYNC` baseline is what Task 1 deliberately breaks and then restores.

**The superseded spec named in §10 step 5 does not exist and never did.** `docs/superpowers/specs/2026-09-10-cross-repo-ref-bump-design.md` is absent from both repos' working trees, absent from every branch of both repos' history (`git log --all --diff-filter=A`), and absent from the filesystem anywhere under `~`. The direction design's own opening line already calls it "the **unlanded** `2026-09-10-cross-repo-ref-bump-design.md`", and §11 preserves why it was discarded — so the reference is accurate history, not a dangling link, and there is nothing to delete. Task 5 records this rather than hunting for it again.

---

## Global Constraints

- **Two repositories.** `pr-review-bot` is `/home/emanresu/pr-review-bot` (the primary working directory); `onboarding-wizard` is `/home/emanresu/onboarding-wizard`. Always state which repo a command runs in. Never `cd` between them inside one compound command — use `git -C` or a separate call.
- **Branch, don't work on `main`.** `cross-repo-contract-stage-5` **already exists in the bot** — this plan file is its first commit, and every bot-side commit here belongs on it. Create the same branch in the wizard before Task 3's first commit. This plan ends with commits on those two branches: **do not merge, do not `git push`.** Merging to `main` additionally requires the `deploy-verify` skill per `CLAUDE.md`; that is the user's call, not this plan's.
- **`CLAUDE.md`'s secret-handling section binds every step here.** Nothing in this plan needs a secret value. Never open `.env` in either repo (the hook blocks it anyway); never run `env`/`printenv`/`set`; never print a `Settings` instance. `.env.config` is non-secret and safe to open, but **no task here needs to touch it** — Task 1 explicitly does not add its throwaway setting to `.env.config` or to `OPERATIONAL_KEYS`.
- **Never modify `.claude/hooks/check_env_access.py` or `redact_output.py`.**
- **Before any commit in either repo:** `uv run pytest -v` green and `uv run ruff check .` clean, in **that** repo. Documentation-only tasks are not exempt — `tests/test_config.py`, `tests/test_gen_docs.py` and the wizard's parity tests all read real files, and a `CLAUDE.md` edit is cheap to verify.
- **Never commit on someone else's behalf.** Both trees are clean as of this writing; if you find pre-existing uncommitted changes in either repo, stop and report rather than sweeping them into a commit.
- **When you correct one sentence in a multi-sentence passage, re-read the whole passage** for internal consistency before moving on (`CLAUDE.md`, Plan-execution hygiene). Task 3 is entirely this failure mode; Task 2 can hit it too.
- ruff `line-length = 100` in both repos. `CLAUDE.md` prose in both repos wraps at ~76 columns — match the surrounding text, do not reflow neighbouring paragraphs.
- Fast iteration in the bot: `uv run pytest -m "not db" -n 4`. The full suite needs the local Postgres, which **is** available (397 db-marked tests pass in ~32s).
- **`ISSUES.md` in the repo the finding belongs to** is where every parked/deferred finding goes before this work is called done — including anything a reviewer rules "no action needed", whose judgment call goes in the **Why parked** line.
- **Per `CLAUDE.md`'s process hygiene:** Task 1 executes a throwaway schema change and shells out to `git` and `uv` across two repos. Treat this plan's snippets with the same suspicion as any other code — matching this plan exactly does not mean this plan was right.

---

### Task 1: Prove a real contract change survives the whole mechanism

§10's closing paragraph, in full:

> Validation of the failure path, not just the plumbing: on a throwaway branch, add a column to `RUNTIME_CONFIG_COLUMNS` and confirm (a) the bot stays green and self-heals a pre-provisioned database, (b) the freshness gate goes red until regenerated, (c) the advisory job reports the wizard as lagging. Then discard the branch. The superseded design's rollout proved only that the plumbing ran on an unchanged schema.

Everything landed so far has been exercised against a schema nobody changed. This task is the only place the rollout ever sees the event it was built for.

**This task produces no lasting code change.** Its deliverable is a written record (Task 5 commits it) and the throwaway branch's deletion.

**Files:**
- Temporarily modify, on a throwaway branch only: `config.py`, `review_queue/store.py`, `review_queue/runtime_config_defaults.py`, `scripts/deploy.py`, plus whatever `scripts/gen_docs` and `scripts/gen_contract` regenerate.
- Temporarily overwrite, uncommitted, then restore: `/home/emanresu/onboarding-wizard/contracts/provisioning.json`.
- Write as you go: `/tmp/claude-1000/-home-emanresu-pr-review-bot/926d7b84-8507-4c2c-bdfb-90316172af98/scratchpad/stage5-validation.md` (the raw log; Task 5 distils it).

**Interfaces consumed (all already on `main`, do not change any of them):**
- `review_queue/store.py::RUNTIME_CONFIG_COLUMNS: tuple[tuple[str, str], ...]` — the declared shape.
- `review_queue/runtime_config_defaults.py::COLUMN_TO_SETTING: dict[str, str]` and `declared_defaults() -> dict[...]`.
- `scripts/deploy.py::_DB_SYNCED_COLUMNS: tuple[str, ...]`.
- `scripts/gen_contract.py` (`python -m scripts.gen_contract`, writes `contracts/provisioning.json`).
- `scripts/check_consumer_contract.py` (`python -m scripts.check_consumer_contract --consumer-root <dir>`; exit 0 `IN_SYNC` / 1 `LAGGING` / 2 `UNCHECKABLE`).
- Wizard: `tests/test_cross_repo_config_ordering.py::test_wizard_seed_leaves_bot_boot_ready`, run with `PR_REVIEW_BOT_PATH` pointing at the bot checkout.

- [ ] **Step 1: Confirm the clean baseline in both repos**

```bash
git -C /home/emanresu/pr-review-bot status --short --branch
git -C /home/emanresu/onboarding-wizard status --short --branch
```

Expected: the bot on `cross-repo-contract-stage-5` (this plan's own branch) and the wizard on `main`, with no output beyond the branch lines. If either tree is dirty, **stop and report** — this task checks branches in and out and would otherwise carry someone else's edits around.

```bash
cd /home/emanresu/pr-review-bot
uv run python -m scripts.check_consumer_contract \
  --consumer-root /home/emanresu/onboarding-wizard --bot-repo-root .
echo "exit=$?"
```

Expected: `IN_SYNC`, `exit=0`. Record it — this is the state Task 1 must restore by the end.

- [ ] **Step 2: Create the throwaway branch**

Branch off `cross-repo-contract-stage-5`, not `main` — the plan file riding along changes nothing, and Step 11 returns here rather than to `main`.

```bash
cd /home/emanresu/pr-review-bot
git checkout -b throwaway/stage5-contract-change-validation
```

- [ ] **Step 3: Add one new column, wired the way a real one would be**

Four hand-maintained lists must all learn about a new `runtime_config` column, and the suite enforces every one of them. A column added to only some of them is not a valid test of the mechanism — it is a test of the tests. Make all four edits, then let the suite tell you if there is a fifth.

`config.py` — add immediately after `dispatcher_notice_sweep_batch_size` (line 156), before the `# --- Draft PRs.` comment block:

```python
    # THROWAWAY (2026-09-10 Stage 5 validation) -- remove with this branch.
    dispatcher_probe_interval_seconds: float = Field(default=11.0, gt=0)
```

`review_queue/store.py` — append to `RUNTIME_CONFIG_COLUMNS` (the tuple ending at `("dispatcher_idle_sleep_seconds", "DOUBLE PRECISION"),`):

```python
    ("dispatcher_probe_interval_seconds", "DOUBLE PRECISION"),
```

`review_queue/runtime_config_defaults.py` — append to `COLUMN_TO_SETTING`:

```python
    "dispatcher_probe_interval_seconds": "dispatcher_probe_interval_seconds",
```

`scripts/deploy.py` — append to `_DB_SYNCED_COLUMNS`:

```python
    "dispatcher_probe_interval_seconds",
```

Why exactly these four, and why nothing else:

- `RUNTIME_CONFIG_COLUMNS` is the declared shape (`store.py:29-39`'s "single source of truth"). `DOUBLE PRECISION` is nullable with no `DEFAULT`, so it satisfies `test_no_runtime_config_column_is_not_null_without_a_default`'s declaration-level invariant and `ADD COLUMN IF NOT EXISTS` can add it to a table that already has rows.
- Without a `Settings` field there is no declared default, so the column would fall into none of the contract's four ownership groups and `test_runtime_config_block_partitions_every_declared_column_exactly_once` would fail. A column with a real default is also the *interesting* case: it exercises the backfill, not just the widen.
- `COLUMN_TO_SETTING` is what `declared_defaults()` walks; `test_every_mapped_column_exists_in_the_schema` and `test_declared_defaults_covers_every_mapped_column_except_none_defaults` pin it.
- `test_deploy_script.py:2215` asserts `set(deploy._DB_SYNCED_COLUMNS) == set(rcd.COLUMN_TO_SETTING)` — set equality, so the fourth edit is not optional.
- **Do not** add `DISPATCHER_PROBE_INTERVAL_SECONDS` to `OPERATIONAL_KEYS`, `_DB_SYNCED_OPERATIONAL_KEYS`, `render.yaml`, or `.env.config`. Nothing couples `_DB_SYNCED_COLUMNS` to `_DB_SYNCED_OPERATIONAL_KEYS` (checked), the OPERATIONAL_KEYS partition test only walks keys already in that set, and `.env.config` is a real local file this validation has no business editing.

- [ ] **Step 4: Regenerate the reference docs (a new `Settings` field changes them)**

```bash
cd /home/emanresu/pr-review-bot
uv run python -m scripts.gen_docs
git diff --stat guide/reference/
```

Expected: `guide/reference/` shows the new field. `tests/test_gen_docs.py::test_config_table_lists_every_settings_field` iterates `Settings.model_fields`, so skipping this reddens the suite for a reason unrelated to what is being validated.

- [ ] **Step 5: (b) — prove the contract freshness gate goes red**

Run CI's `docs`-job steps verbatim, in CI's order (`.github/workflows/ci.yml`):

```bash
cd /home/emanresu/pr-review-bot
uv run python -m scripts.gen_contract
git add -A contracts/
git diff --cached --exit-code contracts/; echo "gate_exit=$?"
```

Expected: **`gate_exit=1`**, and the printed diff adds a `bot_backfilled` entry `{"column": "dispatcher_probe_interval_seconds", "sql_type": "DOUBLE PRECISION", "default": 11.0}`. That non-zero exit is the `docs` job failing — §5.4's freshness gate doing its job. Copy the diff hunk into the scratchpad log.

If `gate_exit=0`, the generator did not pick up the new column: **stop and report**, because §5.4's only guarantee has just failed.

- [ ] **Step 6: Commit the throwaway change, then prove the gate goes green again**

```bash
cd /home/emanresu/pr-review-bot
git add -A
git commit -m "throwaway: add a column to validate the Stage 5 contract mechanism"
uv run python -m scripts.gen_contract
git add -A contracts/
git diff --cached --exit-code contracts/; echo "gate_exit=$?"
git reset
```

Expected: **`gate_exit=0`** — "red until regenerated", both halves shown. The trailing `git reset` unstages; nothing should remain staged when this step ends.

- [ ] **Step 7: (c) — prove the advisory job reports the consumer as lagging**

```bash
cd /home/emanresu/pr-review-bot
uv run python -m scripts.check_consumer_contract \
  --consumer-root /home/emanresu/onboarding-wizard --bot-repo-root .
echo "exit=$?"
```

Expected: verdict **`LAGGING`**, **`exit=1`**, and a detail line naming `runtime_config.bot_backfilled[dispatcher_probe_interval_seconds]` as added. This is the §6.2 job seeing a genuine contract change for the first time.

Note what this run also confirms in passing: the wizard's `.ci/pr-review-bot-ref` pin is unchanged and *older* than this branch, yet the verdict is driven purely by the differing vendored contract — Stage 4's Decision 2 ("an old pin is not lag") holding under a real change rather than a synthetic fixture.

Record the full rendered report in the scratchpad log.

- [ ] **Step 8: (a) — the bot's own suite stays green**

```bash
cd /home/emanresu/pr-review-bot
uv run pytest -v
uv run ruff check .
```

Expected: both clean. If a test fails, it is telling you about a fifth hand-maintained list Step 3 missed — **that is a finding worth recording**, not a nuisance: it is exactly the "how many places must a new column reach" question this validation exists to answer. Fix it on the throwaway branch, `git commit --amend` or add a commit, and note the extra touchpoint in the log.

The two tests that matter most here run under `-m db` and are the direct evidence for (a)'s "self-heals a pre-provisioned database": `tests/test_store_init.py::test_init_pool_widens_a_narrower_pre_provisioned_runtime_config` (its `declared` set is derived from `RUNTIME_CONFIG_COLUMNS`, so it now covers the new column) and `::test_init_pool_backfills_null_columns_from_declared_defaults`.

- [ ] **Step 9: (a) — the real cross-repo chronology, with the new column actually checked**

The wizard's `test_wizard_seed_leaves_bot_boot_ready` derives its non-NULL assertion list from the wizard's **vendored** contract, which does not know about the new column yet. Run it twice, and understand what each run proves.

First, as-is:

```bash
cd /home/emanresu/onboarding-wizard
PR_REVIEW_BOT_PATH=/home/emanresu/pr-review-bot \
  uv run pytest tests/test_cross_repo_config_ordering.py -v
```

Expected: PASS. This proves the widen + backfill still works for the known column set with a wider bot schema in play — necessary, but it does not touch the new column.

Now the run that actually covers it. Temporarily vendor the bot's *new* contract into the wizard's working tree, run **only that one file**, and restore immediately:

```bash
cd /home/emanresu/onboarding-wizard
cp /home/emanresu/pr-review-bot/contracts/provisioning.json contracts/provisioning.json
PR_REVIEW_BOT_PATH=/home/emanresu/pr-review-bot \
  uv run pytest tests/test_cross_repo_config_ordering.py -v
git checkout -- contracts/provisioning.json
git status --short
```

Expected: PASS, and `git status --short` prints nothing.

- **Why this proves (a):** with the new contract vendored, the test's `backfilled` list includes `dispatcher_probe_interval_seconds`; its pre-check asserts the wizard's narrow `_RUNTIME_CONFIG_SCHEMA` does *not* declare it, and its final `SELECT` asserts the bot's boot left it non-NULL. So: the wizard provisions a table that has never heard of the column, the bot widens the table and fills the value from its own declared default, and the dispatcher gate passes. That is §2's two arrows, demonstrated end to end against a real Postgres.
- **Run only that one file during the window.** With a newer contract vendored, `tests/test_bot_contract_parity.py::test_vendored_contract_matches_the_bot_at_the_pinned_ref` correctly goes red (vendored copy ≠ bot at the pinned sha). That is the parity test working, not a problem — but do not leave it red and do not "fix" it by bumping the pin.
- **Restore before doing anything else.** If `git status --short` shows `contracts/provisioning.json` still modified, restore it before continuing; the wizard tree must be clean for Tasks 3 and 4.

- [ ] **Step 10: Confirm each verdict came from the mechanism, not a coincidence**

Do this while the branch still exists. If you want a review of the throwaway diff itself, capture it now with `git diff cross-repo-contract-stage-5...throwaway/stage5-contract-change-validation` into the scratchpad — but the diff is four one-line constant additions plus generated files, and the thing actually worth reviewing is the *validation reasoning*. So: re-read Steps 5, 7 and 9's expected-vs-observed and confirm each verdict came from the mechanism under test rather than from a coincidence (e.g. `LAGGING` caused by a stray edit in the wizard tree rather than by the new column). Record that confirmation in the log.

- [ ] **Step 11: Discard the throwaway branch and verify both repos are back to baseline**

```bash
cd /home/emanresu/pr-review-bot
git status --short              # expect empty (Step 6 committed, Step 6's reset unstaged)
git checkout cross-repo-contract-stage-5
git branch -D throwaway/stage5-contract-change-validation
git status --short --branch     # expect the branch line only, nothing else
uv run python -m scripts.gen_contract
git diff --exit-code contracts/; echo "contract_clean=$?"
uv run python -m scripts.gen_docs
git diff --exit-code guide/reference/; echo "docs_clean=$?"
uv run python -m scripts.check_consumer_contract \
  --consumer-root /home/emanresu/onboarding-wizard --bot-repo-root .
echo "exit=$?"
```

Expected: `contract_clean=0`, `docs_clean=0`, verdict back to **`IN_SYNC`** with `exit=0`, and `git -C /home/emanresu/onboarding-wizard status --short` empty. The checkout must be clean — if git refuses because of uncommitted changes, **stop and report**; do not `-f` your way past it.

- [ ] **Step 12: Write up the outcome (no commit yet)**

Append to the scratchpad log a short, factual record: the exact column added, every file Step 3 (and Step 8, if it found more) had to touch, and for each of (a)/(b)/(c) the command run and the observed verdict/exit code. Task 5 turns this into the design doc's validation note. Write what actually happened, including anything that did not go as this plan predicted.

---

### Task 2: The bot's `CLAUDE.md` — ownership direction, the contract artifact, and the advisory-job carve-out

§9's bot-side section, plus the recurring-failure-mode note §9 asks for "in both", plus the one sentence Stage 4's plan explicitly deferred to here:

> **`CLAUDE.md`'s "never push with a red suite" does not extend to this job.** … (If a `CLAUDE.md` sentence is wanted, it belongs in Stage 5 alongside §9's sections, not here.)

**Files:**
- Modify: `/home/emanresu/pr-review-bot/CLAUDE.md` — insert a new `###` subsection at the end of `## Module boundaries and contracts` (after the `### Contracts` bullet ending `…would silently break that guarantee.`, currently line 192, immediately before `## Conventions` at line 194); and amend the pytest/ruff bullet under `## Conventions` (currently lines 208-211).

**Interfaces:** none — prose only. No test reads this file's contents.

- [ ] **Step 1: Confirm you are on the Stage 5 branch**

It already exists — this plan file is its first commit. Do not try to create it.

```bash
cd /home/emanresu/pr-review-bot
git status --short --branch     # expect: ## cross-repo-contract-stage-5, clean
```

- [ ] **Step 2: Insert the new subsection**

Insert between the last `### Contracts` bullet and the `## Conventions` heading, verbatim:

```markdown
### Cross-repo contract direction (2026-09-10)

**This project owns the schema contract; whoever provisions the database
owns the row.** This repo declares `runtime_config`/`slot_config`'s shape
and every operational default, backfills any column it can derive, and
widens the table itself at boot (`store.init_pool()`'s `ADD COLUMN IF NOT
EXISTS` widen plus its `COALESCE` backfill). The provisioner --
`TovTechOrg/onboarding-wizard` today -- writes only what it uniquely knows:
provider, key slot, model. The bot refuses to start if *that* is missing
(`main.py`'s provider and `slot_config` gates) and never refuses to start
over a column it could have filled itself. These two arrows point opposite
ways on purpose. `ISSUES.md`'s 2026-09-09 incident is what it looks like
when one side silently takes over the other's end: the wizard had to create
the row first -- forced by this project's own boot gate -- which made it the
row's producer while `store.py`'s seeding code still assumed it was, leaving
18 of 22 columns NULL forever on every provisioned deployment.

`contracts/provisioning.json` is the published half of that contract:
generated by `scripts/gen_contract.py` from this repo's own constants
(`OPERATIONAL_KEYS` and its five sync groups, `registry.KEY_INDEX_COLUMNS`,
`RUNTIME_CONFIG_COLUMNS`, `runtime_config_defaults.COLUMN_TO_SETTING`) and
byte-compared in CI's `docs` job, then vendored verbatim into the consumer.
Change a column, a default, or an env-var placement and the contract is
regenerated in the same commit. It carries names, placements, SQL types and
non-secret operational defaults only -- it reads the `Settings` **class**'s
declared defaults, never the module-level `settings` instance, exactly as
`gen_docs.py` does and for the same reason (see the secret-handling section
at the top of this file).

The direction is what makes this deadlock-free, so keep it: **this repo's
contract checks are all local, and it never blocks on the consumer.** A
contract change lands here green on its own; the consumer catches up in one
commit afterwards. Its lag is reported by a scheduled advisory job, never by
a blocking check -- see the `## Conventions` note below.

**A store-layer docstring that asserts a caller-set invariant ("the only
caller always writes the full pair") is a validation gap waiting for its
second caller.** This has now cost two incidents in `store.py`: the
2026-09-09 seeding assumption above, and `set_cooldown_override`/
`set_usage_cap_override`'s "there is no partial-field write to merge with",
which `dashboard/environment.py::_apply_config_patch` had already falsified
-- leaving a UI path that wrote an unusable cooldown triple or a
never-parsing usage-cap reset time and reported it as `applied`. Validate in
one shared predicate every writer calls (`cooldown_config.problems()`,
`usage_cap_config.problems()`, `dispatcher_tuning_config.problems()`,
consumed by `deploy.py --sync-config-db`, the dashboard PATCH, and the boot
gate alike), not in prose about who calls you.
```

- [ ] **Step 3: Amend the pytest/ruff convention bullet**

The bullet currently reads:

```markdown
- **Before pushing, always run the full test suite (`uv run pytest -v`) and
  ruff (`uv run ruff check .`), and fix whatever either finds.** Never push
  with a red suite or an unresolved lint error, and never skip either check
  because a change "looks" too small to affect them.
```

Append these lines to that same bullet (keep it one bullet; do not start a new one):

```markdown
  This rule covers `pytest`, `ruff`, and CI's blocking `lint-and-test`/`docs`
  jobs. It deliberately does **not** extend to
  `.github/workflows/consumer-contract-lag.yml`, the scheduled advisory
  consumer-lag job: a red run there means the consumer has not vendored this
  repo's latest contract yet, which is the normal, transient state between a
  contract change landing here and the consumer's catch-up commit. Gating a
  push on it would invert the ownership direction above and reintroduce
  exactly the deadlock the superseded reciprocal-pin design died of.
```

- [ ] **Step 4: Re-read the whole of both edited passages**

Read `## Module boundaries and contracts` from its heading through the end of the new subsection, and the entire amended `## Conventions` bullet. Check specifically: nothing above now contradicts the new text (the `### Contracts` bullets describe webhook/provider/model contracts and should be untouched), the new subsection's forward reference to "`## Conventions` note below" resolves, and the 2026-09-09 incident is described the same way here as in `ISSUES.md`.

- [ ] **Step 5: Verify**

```bash
cd /home/emanresu/pr-review-bot
uv run pytest -v
uv run ruff check .
git diff --stat
```

Expected: green, clean, and `git diff --stat` shows `CLAUDE.md` only.

- [ ] **Step 6: Commit**

```bash
cd /home/emanresu/pr-review-bot
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
Write the bot-owns-schema, provisioner-owns-row direction into CLAUDE.md

Spec section 9. Also records the recurring store.py failure mode (a
docstring asserting a caller-set invariant is a validation gap waiting
for its second caller, twice now) and the carve-out Stage 4 deferred to
here: "never push with a red suite" does not extend to the scheduled
advisory consumer-lag job, whose red state is the normal interval
between a contract change here and the consumer's catch-up commit.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SDbwxZLhhjZ9NK38PeDEnm
EOF
)"
```

---

### Task 3: The wizard's `CLAUDE.md` — the new section, and the prose Stage 3 falsified

Two halves, and the second is the one a reviewer should scrutinise. §9 asks for a new top-level section in the wizard. But Stage 3 changed the wizard's code without touching its `CLAUDE.md`, so the sub-project-6 section now states, as current fact, three things that are false:

1. *"the 9 dispatcher knobs get a real value from that project's own first-boot seeding (`review_queue/store.py::_seed_runtime_config_defaults`)"* — false twice over. That function was **removed** by the 2026-09-09 fix, and Stage 1 replaced it with a widen-and-backfill that is not seeding (§3.2: seeding asked "does row 1 exist?", the backfill asks per column).
2. *"`runtime_config`'s duplicate must be the FULL column set, not just the columns this wizard writes, or that project's own `CREATE TABLE IF NOT EXISTS` on first boot would find the table already exists and never widen it"* — this requirement **expired** with §3.1, and Stage 3 acted on that: `_RUNTIME_CONFIG_SCHEMA` is now deliberately narrow (six columns) and `router.py:274` already carries a comment saying so. The `CLAUDE.md` passage still demands the opposite of what the code does.
3. *"Keep this dict in sync with the sibling project's config by hand."* — still hand-written, but no longer *only* by hand: `tests/test_bot_contract_parity.py` now asserts it against the vendored contract.

Leaving these is worse than never having written them: they are the authoritative instructions the next agent reads before touching `router.py`'s provisioning writes, and each one points at the pre-incident behaviour.

**Files:**
- Modify: `/home/emanresu/onboarding-wizard/CLAUDE.md` — new `##` section inserted immediately before `## Rules` (currently line 297); corrections inside `## What sub-project 6 (Render service creation + deploy, final) adds to these rules` (the `_GENERIC_OPERATIONAL_ENV_DEFAULTS` bullet at ~709-723 and the `slot_config`/`runtime_config` seeding bullet at ~724-780).

**Interfaces:** none — prose only.

- [ ] **Step 1: Create the branch**

```bash
cd /home/emanresu/onboarding-wizard
git status --short          # must be empty; Task 1 Step 9 restored contracts/provisioning.json
git checkout -b cross-repo-contract-stage-5
```

- [ ] **Step 2: Insert the new top-level section immediately before `## Rules`**

Placement rationale, so it is not re-litigated: this is a dated topical section of the same kind as `## Docker image: no chown -R` and `## The invariant this service now protects`, and it belongs directly before `## Rules` because everything the sub-project-6 section says about `runtime_config` is an *application* of it — a reader reaches the direction statement before the bullets that implement it.

```markdown
## The cross-repo contract with the review-engine project (2026-09-10)

**`pr-review-bot` owns the schema contract; this wizard owns the row.** That
project declares `runtime_config`/`slot_config`'s shape and every
operational default, backfills any column it can derive from its own
`Settings` defaults, and widens the table itself at boot (`ADD COLUMN IF NOT
EXISTS`, then a `COALESCE` upsert that fills only NULLs and can never
clobber a value this wizard wrote). This wizard writes only what it uniquely
knows -- which provider the visitor chose, which key slot, which model --
and that project refuses to start if *that* is missing. These two arrows
point opposite ways on purpose.

`ISSUES.md`'s 2026-09-09 incident is what it looks like when one side
silently takes over the other's end: this wizard had to create the
`runtime_config` row first (forced by the bot's own provider/slot_config
boot gate), which made it the row's producer while the bot's seeding code
still assumed it was -- 18 of 22 columns NULL forever, and every PR review
on every wizard-provisioned deployment stuck behind a "Dispatcher
configuration issue" comment that never resolved. The mechanism was
row-creation *order*, not the migration of the tuning knobs into the
database.

The mechanical half is `contracts/provisioning.json`, a verbatim copy of a
file `pr-review-bot` generates from its own constants and publishes for
exactly this purpose:

- **Never edit the vendored copy by hand**, and never bump
  `.ci/pr-review-bot-ref` on its own.
  `uv run python -m scripts.update_bot_contract` rewrites **both together or
  neither** -- they are two halves of one fact (which bot contract we are
  built against), and it refuses to write either if
  `tests/test_bot_contract_parity.py` goes red against the extracted copy.
  It resolves `origin/main`, never a local `HEAD` or a feature-branch tip.
- **These are subset/superset checks, never equality checks.** This wizard's
  DDL is deliberately narrower than the bot's declared shape, and the bot's
  boot-time widen-and-backfill is what makes that narrowness harmless. The
  tests assert coverage in both directions that matter -- we write
  everything the bot's boot gate requires, and we push nothing the bot reads
  only from the database -- not identity. An equality assertion across two
  repos cannot be satisfied by either one alone, which is why the earlier
  reciprocal-pin design was discarded.
- **A new hand-maintained duplicate of a bot fact ships with its parity
  assertion in the same commit.** `_LLM_ENV_VAR_NAMES`, `_KEY_INDEX_COLUMNS`
  and `_GENERIC_OPERATIONAL_ENV_DEFAULTS` stay hand-written precisely
  because the vendored contract is what catches a rename in them. A
  duplicate with no assertion is the 2026-09-09 shape all over again.
- **Being briefly behind is normal, not a breakage.** The bot's own CI never
  blocks on this repo; a bot contract change lands there first and a
  scheduled advisory job reports us as lagging until
  `update_bot_contract.py` runs here. Catching up is one commit and is never
  urgent enough to hand-edit either file.

**A docstring that asserts a caller-set invariant ("the only caller always
writes the full pair") is a validation gap waiting for its second caller.**
This has now cost two incidents in `pr-review-bot`'s `store.py`, and
`router.py`'s provisioning writes are the same shape: a single caller today,
prose standing in for a check. Validate in a predicate every writer calls,
not in prose about who calls you.
```

- [ ] **Step 3: Correct the `_GENERIC_OPERATIONAL_ENV_DEFAULTS` bullet**

In that bullet, replace this clause:

```
all of those are now DB-only over there (no Render
  env var at all) and are dropped from this dict entirely rather than
  pushed as `""` — the 9 dispatcher knobs get a real value from that
  project's own first-boot seeding (`review_queue/store.py::
  _seed_runtime_config_defaults`), and `VERTEX_GCP_LOCATION` (along with
  model and, for vertex, project) is seeded by this wizard directly into
  `slot_config` instead — see below.
```

with:

```
all of those are now DB-only over there (no Render
  env var at all) and are dropped from this dict entirely rather than
  pushed as `""` — the 9 dispatcher knobs get a real value from that
  project's own boot-time backfill (`review_queue/store.py::init_pool`
  fills every NULL `runtime_config` column from its own declared `Settings`
  defaults; the older `_seed_runtime_config_defaults` was removed on
  2026-09-09 and is not what fills them any more — see the cross-repo
  contract section above), and `VERTEX_GCP_LOCATION` (along with
  model and, for vertex, project) is seeded by this wizard directly into
  `slot_config` instead — see below.
```

and replace the bullet's closing sentence:

```
Keep this dict in
  sync with the sibling project's config by hand.
```

with:

```
This dict is still hand-written, but no longer
  hand-*checked*: `tests/test_bot_contract_parity.py` asserts its keys and
  placement against `contracts/provisioning.json`, so a rename or a
  placement move on the bot side fails a test here instead of silently
  pushing a name nothing reads.
```

- [ ] **Step 4: Correct the `runtime_config` DDL-duplication claim**

In the `slot_config`/`runtime_config` seeding bullet, replace:

```
runs both tables' `CREATE TABLE IF NOT
  EXISTS` (duplicated by hand from that project's `store.py`'s `_SCHEMA`/
  `RUNTIME_CONFIG_COLUMNS`, same convention as `_LLM_ENV_VAR_NAMES`/
  `_GENERIC_OPERATIONAL_ENV_DEFAULTS` — `runtime_config`'s duplicate must be
  the FULL column set, not just the columns this wizard writes, or that
  project's own `CREATE TABLE IF NOT EXISTS` on first boot would find the
  table already exists and never widen it) before the `INSERT ... ON
  CONFLICT DO UPDATE`s,
```

with:

```
runs both tables' `CREATE TABLE IF NOT
  EXISTS` before the `INSERT ... ON CONFLICT DO UPDATE`s. As of 2026-09-10
  `runtime_config`'s DDL here is deliberately NARROW — only the columns
  `contracts/provisioning.json` lists as `provisioner_required` /
  `provisioner_required_one_of`, i.e. the ones this wizard actually writes.
  It used to have to be that project's FULL column set, because a
  `CREATE TABLE IF NOT EXISTS` from a narrower creator would leave the bot's
  own boot unable to widen the table; that project now widens it itself
  (`ADD COLUMN IF NOT EXISTS` per declared column, then a `COALESCE`
  backfill), so the requirement expired and the copied 22-column DDL and
  15 copied default values went with it. See `router.py`'s own comment above
  `_RUNTIME_CONFIG_SCHEMA` and the cross-repo contract section above.
  `_SLOT_CONFIG_SCHEMA` is still the full six columns — it could shrink too,
  but every column is either required or optional-and-written, so there is
  nothing to gain; that is deliberate, not an oversight.
```

- [ ] **Step 5: Re-read both edited bullets end to end**

This is the step the `CLAUDE.md` hygiene rule exists for: a targeted fix to one clause is exactly the edit that leaves a contradiction elsewhere in the same passage. Read each bullet whole. Check in particular that nothing still claims the wizard copies defaults or a full column set, that `_SLOT_CONFIG_SCHEMA`'s treatment is stated once and consistently, and that the sentence about writing `slot_index = 0`/`{provider}_key_index = 0` still reads correctly after the surrounding text changed.

- [ ] **Step 6: Sweep the rest of the file for the same class of stale claim**

```bash
cd /home/emanresu/onboarding-wizard
grep -n -i "_seed_runtime_config_defaults\|FULL column set\|by hand\|22 column\|first-boot seeding" CLAUDE.md
```

Expected after Steps 3-4: no hit still asserting the retired behaviour. Any remaining hit is either already-correct text or a fourth stale claim — fix it here, or, if fixing it is genuinely out of this plan's scope, log it in `ISSUES.md` rather than leaving it unrecorded.

- [ ] **Step 7: Verify**

```bash
cd /home/emanresu/onboarding-wizard
uv run pytest -v
uv run ruff check .
git diff --stat
```

Expected: green, clean, `CLAUDE.md` only. (`tests/test_cross_repo_config_ordering.py` will skip unless `PR_REVIEW_BOT_PATH` is set — that is correct local behaviour, and it is not a skip-reads-as-pass problem because `CI` is unset. Task 1 Step 9 already ran it for real.)

- [ ] **Step 8: Commit**

```bash
cd /home/emanresu/onboarding-wizard
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
State the cross-repo contract direction, and retire the prose Stage 3 falsified

Adds the bot-owns-schema / wizard-owns-row section (pr-review-bot's spec
section 9) and corrects three sub-project-6 claims that Stage 3's code
changes had silently made false: the 9 dispatcher knobs come from the
bot's boot-time backfill rather than a _seed_runtime_config_defaults that
no longer exists; runtime_config's DDL here is now deliberately narrow
rather than required to be the bot's full column set; and
_GENERIC_OPERATIONAL_ENV_DEFAULTS is checked against the vendored
contract rather than kept in sync purely by hand.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SDbwxZLhhjZ9NK38PeDEnm
EOF
)"
```

---

### Task 4: Repair the orphaned parked-issue heading in the wizard's `ISSUES.md`

Found while surveying Stage 3's output for this plan, and it belongs to Stage 5 because Stage 5 is what closes the branch that caused it. Commit `0f59b75` ("Log the type/constraint-parity gap left by Stage 3's contract vendoring") inserted its new entry by **replacing** the following entry's `###` heading instead of inserting before it. The result: two distinct parked issues are fused under one title, and the `_seed_slot_config` `CREATE TABLE IF NOT EXISTS` race entry has silently lost its name — it now reads as a second `**Found during:**` line belonging to the contract-parity entry.

A parked issue whose title vanished is a parked issue nobody will ever find again, which defeats the entire purpose of the section.

**Files:**
- Modify: `/home/emanresu/onboarding-wizard/ISSUES.md` (the boundary is currently at line 145/146).

- [ ] **Step 1: Confirm the damage**

```bash
cd /home/emanresu/onboarding-wizard
sed -n '140,150p' ISSUES.md
git show 0f59b75 -- ISSUES.md | head -30
```

Expected: the current file shows a `- **Follow-up:** When pr-review-bot next revises …` line followed directly by `- **Found during:** 2026-09-09 code-correctness review of the slot_config/env-var-DB rework (commit 9de5f04) …` with no `###` between them, and the commit diff shows the removed heading line.

- [ ] **Step 2: Restore the heading**

Insert this line (with one blank line before it and none after) between the contract-parity entry's `**Follow-up:**` line and the orphaned `- **Found during:** 2026-09-09 …` line, restoring it exactly as `0f59b75` removed it:

```markdown
### `_seed_slot_config`'s `CREATE TABLE IF NOT EXISTS` isn't race-free against the deployed bot's own concurrent `store.init_pool()`
```

- [ ] **Step 3: Verify the section's structure is now uniform**

```bash
cd /home/emanresu/onboarding-wizard
awk '/^### /{h=$0; n=0} /^- \*\*Found during:\*\*/{n++; if(n>1) print "TWO Found-during under: " h}' ISSUES.md
git diff --stat
```

Expected: no output from `awk` (every `###` entry has exactly one `**Found during:**` line), and `ISSUES.md` as the only changed file.

- [ ] **Step 4: Commit**

```bash
cd /home/emanresu/onboarding-wizard
git add ISSUES.md
git commit -m "$(cat <<'EOF'
Restore the parked-issue heading 0f59b75 overwrote

The Stage 3 contract-parity entry was inserted over the following
entry's ### line rather than before it, fusing two parked issues under
one title and leaving the _seed_slot_config CREATE TABLE race with no
heading at all -- unfindable, which is the one thing this section exists
to prevent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SDbwxZLhhjZ9NK38PeDEnm
EOF
)"
```

---

### Task 5: Record the validation outcome, close out the supersession, and hand back

**Files:**
- Modify: `/home/emanresu/pr-review-bot/docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md` — append a dated validation note at the end of §10, after the "Validation of the failure path" paragraph and before `## 11.`.
- Possibly modify: `/home/emanresu/pr-review-bot/ISSUES.md` (only if Task 1 or a reviewer produced a parked finding).

- [ ] **Step 1: Append the validation note to §10**

Write it from Task 1's scratchpad log, **after** that task ran — not from this plan's predictions. Use this shape, filling every bracket with what was actually observed:

```markdown
**Validated 2026-09-10.** Run on a throwaway branch that added
`dispatcher_probe_interval_seconds DOUBLE PRECISION` (declared default
`11.0`) to `RUNTIME_CONFIG_COLUMNS`, with the four hand-maintained lists a
new column must reach: `config.py`'s `Settings`,
`store.RUNTIME_CONFIG_COLUMNS`, `runtime_config_defaults.COLUMN_TO_SETTING`,
and `deploy._DB_SYNCED_COLUMNS`[, plus <anything Step 8 uncovered>].

- **(a) The bot stays green and self-heals a pre-provisioned database.**
  `uv run pytest -v` and `ruff check .` [outcome]. The proof that matters is
  cross-repo: with the new contract vendored into the wizard,
  `test_wizard_seed_leaves_bot_boot_ready` [outcome] — the wizard provisions
  a six-column table that has never heard of the new column, and after the
  bot's own `init_pool()` the column reads back non-NULL at its declared
  default, with `dispatcher_tuning_config.problems()` empty.
- **(b) The freshness gate goes red until regenerated.** CI's two `docs`-job
  steps exited [n] against the stale committed contract and [n] after
  `gen_contract` was re-run and committed.
- **(c) The advisory job reports the consumer as lagging.**
  `check_consumer_contract` returned [verdict]/exit [n], naming
  `runtime_config.bot_backfilled[dispatcher_probe_interval_seconds]`. The
  consumer's pin was unchanged throughout, confirming the verdict is driven
  by the vendored contract and not by pin age.

The branch was discarded; both repositories returned to `IN_SYNC` with clean
trees and byte-identical regenerated artifacts.
```

- [ ] **Step 2: Record the supersession outcome**

§10 step 5 says "delete the superseded spec". There is nothing to delete: `2026-09-10-cross-repo-ref-bump-design.md` never landed in either repo's working tree or history. Confirm once more, then leave the design doc's §1/§11 references intact — they are the only surviving record of why that approach was rejected, and both already describe it as unlanded.

```bash
cd /home/emanresu/pr-review-bot
git log --all --oneline --diff-filter=A -- 'docs/superpowers/specs/*ref-bump*'
git -C /home/emanresu/onboarding-wizard log --all --oneline --diff-filter=A -- 'docs/superpowers/specs/*ref-bump*'
grep -rn "ref-bump" --include="*.md" . /home/emanresu/onboarding-wizard 2>/dev/null | grep -v '^\./\.git/'
```

Expected: both `git log`s empty; the only `grep` hit is the direction design's own line 3. If any of that is not true, the file **does** exist somewhere and step 5 has real work — delete it and say so.

Add one line to the validation note recording this, so the next reader does not repeat the search:

```markdown
(§10 step 5's "delete the superseded spec" was a no-op: the reciprocal-pin
design was never committed to either repository. §1 and §11 keep the record
of why it was discarded.)
```

- [ ] **Step 3: Log any parked findings**

Every deferred finding from Task 1 or from the review — including anything ruled "no action needed", whose reasoning goes in the **Why parked** line — goes into the `ISSUES.md` of the repo it concerns, in that section's existing format, before this work is called done. Not the SDD ledger, not the session transcript.

- [ ] **Step 4: Final verification in both repos**

```bash
cd /home/emanresu/pr-review-bot
uv run pytest -v && uv run ruff check . && git status --short --branch
cd /home/emanresu/onboarding-wizard
uv run pytest -v && uv run ruff check . && git status --short --branch
```

Expected: both suites green, both lints clean, both trees clean on `cross-repo-contract-stage-5`.

- [ ] **Step 5: Commit**

```bash
cd /home/emanresu/pr-review-bot
git add docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md ISSUES.md
git commit -m "$(cat <<'EOF'
Record what the end-to-end contract validation actually showed

Spec section 10's closing paragraph, run for real: a throwaway column
added to RUNTIME_CONFIG_COLUMNS, then the freshness gate, the advisory
consumer-lag job, and the live cross-repo widen-and-backfill chronology
each observed against it. Written after the run rather than drafted
against an assumed outcome. Also records that step 5's "delete the
superseded spec" was a no-op -- that design never landed anywhere.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SDbwxZLhhjZ9NK38PeDEnm
EOF
)"
```

- [ ] **Step 6: Report, do not merge**

Report to the user: what Task 1 observed for (a)/(b)/(c), what changed in each `CLAUDE.md`, the `ISSUES.md` repair, any parked findings, and that two branches named `cross-repo-contract-stage-5` are sitting unmerged and unpushed in the two repos. Merging either to `main` requires the `deploy-verify` skill and is the user's decision.

---

## Notes for the reviewer

The interesting review surface here is small and specific — most of this plan is prose. Concentrate on:

1. **Did Task 1's verdicts come from the mechanism, or from a coincidence?** A `LAGGING` caused by a stray uncommitted edit in the wizard tree, or a red freshness gate caused by `gen_docs` rather than `gen_contract`, would look identical in a log skimmed quickly. Check that the reported diff lines actually name the new column.
2. **Is the (a) evidence real?** The as-is run of `test_wizard_seed_leaves_bot_boot_ready` does **not** cover the new column (its assertion list comes from the vendored contract). Only the second run, with the new contract temporarily copied in, does. If only the first run happened, (a) is unproven.
3. **Did the wizard's tree get restored?** `contracts/provisioning.json` must be byte-identical to its committed state, and `.ci/pr-review-bot-ref` must be untouched. A vendored contract left overwritten would break the wizard's parity tests in a way that looks like a Stage 3 bug.
4. **Are the `CLAUDE.md` claims true of the code as it stands today?** Every factual assertion in Tasks 2 and 3 names a real symbol — `store.init_pool()`, `cooldown_config.problems()`, `_apply_config_patch`, `update_bot_contract.py`, `_RUNTIME_CONFIG_SCHEMA`'s narrowness. Spot-check them; documentation that overstates what landed is the exact failure this stage is fixing.
5. **Task 3, Step 5's whole-passage re-read.** The most likely defect in this entire plan is a sub-project-6 bullet left half-corrected — a clause fixed, a neighbouring sentence still asserting the retired behaviour.
6. **No scope creep into Stage 1-4 code.** Nothing outside the two `CLAUDE.md` files, the wizard's `ISSUES.md`, the design doc's §10, and (temporarily, on a deleted branch) Task 1's four constants should have changed.
