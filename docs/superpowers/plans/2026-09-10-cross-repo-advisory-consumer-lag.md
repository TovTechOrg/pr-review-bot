> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one scheduled, never-blocking job that checks out `TovTechOrg/onboarding-wizard` at `main`, compares its vendored `contracts/provisioning.json` against this repo's *currently generated* contract, and reports a lagging consumer — so a bot contract change that the wizard has not yet picked up becomes visible within a day instead of surfacing as a misconfigured provisioned deployment.

**Architecture:** Two pieces with a hard seam between them. `scripts/check_consumer_contract.py` holds all the comparison logic as pure functions over *strings and dicts* — no network, no GitHub, no filesystem in the core — so it is unit-testable in the normal suite. `.github/workflows/consumer-contract-lag.yml` is a separate workflow file (never `ci.yml`) whose only triggers are `schedule` and `workflow_dispatch`; it does the two checkouts and hands the script a directory. The script writes a markdown report to `$GITHUB_STEP_SUMMARY`, emits workflow annotations, and exits 0 (in sync) / 1 (lagging) / 2 (the check could not be performed).

**Tech Stack:** Python 3.12, `json`/`hashlib`/`subprocess`/`argparse` (stdlib), pytest (`-n 4`), PyYAML (tests only), ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md` — this plan implements **Stage 4 only** (§10 rollout step 4: §6.2's advisory job). Read §5.4, §5.5, §6.2, §6.3 and §10's final paragraph before starting.

---

## Open decisions the user may want to weigh in on

The design specifies *that* the job "reports a lagging consumer" but not *where* the report goes. These are this plan's calls, made explicitly so they can be overridden before implementation rather than discovered afterwards.

**Decision 1 — the report goes to `$GITHUB_STEP_SUMMARY`, and a lagging consumer fails the scheduled run.**

Chosen because it needs no token, no secret, no new permission beyond `contents: read`, and no deduplication logic, and because GitHub already emails the workflow file's last committer when a *scheduled* workflow fails. That is a free, durable notification channel in a project whose entire posture (see `cost.md`, `CLAUDE.md`'s secret-handling section) is "add no third-party integration and no credential you don't have to."

Three outcomes, deliberately distinguished by exit code so "the consumer lags" never reads the same as "the check is broken":

| Exit | Verdict | Meaning |
|---|---|---|
| 0 | `IN_SYNC` | The vendored copy is byte-identical to the generated contract. |
| 1 | `LAGGING` | The consumer has not caught up. **Expected and transient** — it is the normal state between a bot contract change and the wizard's `update_bot_contract.py` run. |
| 2 | `UNCHECKABLE` | The comparison could not be performed: the sibling repo is unreachable/private, or *this* repo's committed contract is stale. A genuine tooling failure. |

**The known cost of this choice:** after any contract change the run is legitimately red until the wizard lands its commit, so a persistent red scheduled workflow can habituate into "that's just normal" — the inverse of §6.3's "CI skips must not read as passes." Mitigations built into the plan: the workflow's `name:` says "(advisory)", the step summary leads with which of the three outcomes it is and what closes it, `workflow_dispatch` lets you re-run on demand the moment the wizard catches up rather than waiting a day, and **no README badge is added** (a red badge next to the CI badge would read as "the bot is broken", which is precisely wrong).

**Alternatives, and how to switch:**
- *(A) Warn-only — always exit 0, report as a `::warning::` annotation.* Quieter, but effectively invisible: nobody opens a green run. Task 2 ships a `--never-fail` flag, so switching is a one-word edit to the workflow.
- *(B) Open/update a single tracking issue with a fixed title.* More durable and assignable, and immune to the habituation problem. Costs `issues: write`, dedup logic (`gh issue list --search` then create-or-comment), and issue noise. This is the natural upgrade **if** the fail-the-run signal proves too noisy or too quiet; the plan is structured so it is one added step gated on the compare step's exit code, not a rewrite.
- *(C) Slack.* Rejected: it requires a new webhook secret in a repo with no such secret today, and every rule in `CLAUDE.md`'s secret-handling section then applies to it — a disproportionate cost for a once-a-day advisory ping.

**Decision 2 — the consumer's `.ci/pr-review-bot-ref` pin is read, but is report context only and never an input to the verdict.** An old pin is not lag: the wizard can be pinned 40 commits back and still be perfectly in sync if none of those commits touched the contract. Only a *differing vendored contract* is lag. Getting this backwards would produce a permanently red job.

**Decision 3 — a missing vendored copy is `LAGGING`, not an error.** At the moment Stage 4 lands, Stage 3 may not have; the consumer will have no `contracts/provisioning.json` at all. That is exactly "a lagging consumer" and must produce a clean, correct report — this is what makes Stage 4 independent of Stage 3, as §10's ordering requires.

---

## Global Constraints

- **Stages 1 and 2 are landed on `main`.** `scripts/gen_contract.py`, `contracts/provisioning.json`, `tests/test_provisioning_contract.py`, and `ci.yml`'s `docs`-job freshness steps all exist. This plan **reads** them and changes none of them. If a task seems to require editing one, stop and report.
- **`ci.yml` must not be touched at all.** `tests/test_ci_workflow.py::test_the_contract_freshness_check_needs_no_database_or_sibling_checkout` asserts the string `onboarding-wizard` appears nowhere in `ci.yml`. That existing test is the mechanical enforcement of §6.2's "never blocking" — the advisory job goes in a **separate workflow file** or that test goes red. Do not weaken it.
- **Never blocking, concretely.** The new workflow's triggers are `schedule` and `workflow_dispatch` **only**. No `push`, no `pull_request`, no `workflow_run`, no `needs:` relationship with anything in `ci.yml` (impossible across files anyway). Because it never runs on a commit or a PR, it can never produce a check run against one, which in turn means it **cannot be selected as a required status check in branch protection** — the property is structural, not conventional. It also has no bearing on `ci.yml`'s `pages` job, which gates on `needs: [lint-and-test, docs]`.
- **`CLAUDE.md`'s "never push with a red suite" does not extend to this job.** That rule is about `uv run pytest` / `uv run ruff check .` and the blocking CI job. A red *advisory* scheduled run means the consumer has not caught up — the exact thing §6.2 says must not gate a bot push. Say so in the workflow's own comments. (If a `CLAUDE.md` sentence is wanted, it belongs in Stage 5 alongside §9's sections, not here.)
- **Secret handling.** The contract carries names, placement words, SQL types and non-secret operational defaults only (§5.3, pinned by `test_the_contract_never_contains_a_configured_value`), so printing its full diff into a **public** workflow log is safe. Nothing else may be printed: no `env`/`printenv`, no `set -x` around anything env-bearing, no `curl -v`. The script must never read `config.settings` — Task 1 adds the same AST import guard `gen_contract` already carries.
- **The script must never traceback out.** An unexpected exception *is* the `UNCHECKABLE` verdict: catch it, report it, write the summary, exit 2. An advisory job whose only product is a report must always produce one.
- **Out of scope, do not touch:** anything in `onboarding-wizard` (Stage 3, and it isn't in this working directory); §9's `CLAUDE.md` sections (Stage 5); `render.yaml`; `dashboard/static/`.
- **Before pushing:** `uv run pytest -v` and `uv run ruff check .` clean, and per `CLAUDE.md` any push to `main` needs the `deploy-verify` skill. This plan ends with commits on a branch — **never `git push`** without being asked.
- ruff `line-length = 100`. Fast iteration: `uv run pytest -m "not db" -n 4` (nothing here touches Postgres).
- **Per `CLAUDE.md`'s process-hygiene rules:** this plan hands the implementer verbatim code that performs a cross-repo checkout and shells out to `git`. Run the `code-review` skill against Task 1's and Task 3's diffs *as part of finishing those tasks*, not deferred to a whole-branch review. Matching this plan exactly does not mean this plan was right.

---

### Why `actions/checkout` and not a raw fetch

The design already names `actions/checkout`; this records why that is the right call rather than treating it as arbitrary, because the alternatives fail in ways that corrupt the *verdict*, not just the ergonomics.

- **`raw.githubusercontent.com` fetch.** Rejected. It is CDN-cached, so "current `main`" becomes approximate — a false `IN_SYNC` right after the wizard lands its commit, or a false `LAGGING` right after the bot lands one. Worse, a 404 body is an HTML error page, so "the consumer hasn't vendored it yet", "the repo went private", and "the repo was renamed" all arrive as the same unparseable blob — collapsing precisely the `LAGGING`/`UNCHECKABLE` distinction Decision 1 depends on. It also needs hand-rolled status/retry/timeout handling in shell, where a network flake silently becomes a wrong report.
- **GitHub Contents API.** Rejected. Unauthenticated it is 60 requests/hour per IP, and Actions runner IPs are shared and NATted, so it can be rate-limited unpredictably. Authenticating does not help: this workflow's automatic `GITHUB_TOKEN` is scoped to *this* repository and grants nothing on the sibling, so it would take a PAT or App installation token — a new secret, which §6.2 explicitly avoids.
- **Hand-rolled `git clone`.** Strictly worse than the action: no sparse/shallow ergonomics, no pinned action version, more shell to get wrong.
- **`actions/checkout` (chosen).** No token for a public repo. A sparse, non-cone checkout of two paths keeps it cheap. Everything the script then reads is a real file from the consumer's real `main` tree — so malformed JSON unambiguously means "the consumer's vendored copy is corrupt" and never "we got an error page". It picks up `.ci/pr-review-bot-ref` in the same step for free. And when either repo goes private the step fails with an unmistakable auth error rather than degrading into a confidently wrong verdict — which is exactly the event §6.2 says is "worth a workflow comment."

---

### Task 1: The comparison logic — `scripts/check_consumer_contract.py`

Pure functions over strings and dicts. No network, no `git`, no `argparse`, no `$GITHUB_STEP_SUMMARY` yet — Task 2 adds all of that. This split is the whole reason the advisory job's *logic* gets real test coverage while its *trigger* only gets a structural check.

The diff is **generic**, not four hand-written per-block comparators. The contract's shape is versioned and will grow blocks (§5.2's `contract_version`), and a comparator that enumerates today's four blocks silently stops reporting the fifth. Flatten both documents to dotted paths, then set-diff:

- dicts recurse (`env_vars.GITHUB_TARGET_REPO.placement`);
- a list of dicts each carrying a `column` key is keyed **by column name**, not by index (`runtime_config.bot_backfilled[cooldown_base_seconds].default`), so inserting a column reports one added column instead of shifting every entry after it;
- everything else — including lists of plain strings like `provisioner_required`, whose *order* is load-bearing and asserted elsewhere — is a leaf compared with `==`.

**Files:**
- Create: `scripts/check_consumer_contract.py`
- Create: `tests/test_consumer_contract_check.py`
- Read (do not modify): `scripts/gen_contract.py`, `contracts/provisioning.json`, `tests/test_provisioning_contract.py` (for the AST-guard and sentinel patterns)

**Interfaces produced:**
- `CONSUMER_REPO: str` — `"TovTechOrg/onboarding-wizard"`. Single-sourced: Task 3's workflow test asserts the checkout step's `repository:` equals this constant, so the two cannot drift.
- `CONSUMER_PIN_PATH: str` — `".ci/pr-review-bot-ref"` (§5.5).
- `CONSUMER_CONTRACT_PATH: str` — aliased to `gen_contract.CONTRACT_PATH`; the same relative path in both repos by design.
- `IN_SYNC`, `LAGGING`, `UNCHECKABLE: str` verdicts and `EXIT_CODE: dict[str, int]` = `{IN_SYNC: 0, LAGGING: 1, UNCHECKABLE: 2}`.
- `Report` — frozen dataclass: `verdict`, `headline`, `details: list[str]`, `bot_version`/`consumer_version: int | None`, `bot_digest`/`consumer_digest: str | None`, `pin: str | None`, `pin_context: str | None`.
- `differences(bot: dict, consumer: dict) -> list[str]`
- `compare(bot_text: str, consumer_text: str | None, *, pin: str | None = None, pin_context: str | None = None) -> Report`
- `render_report(report: Report) -> str`

`compare()`'s decision order, each pinned by a test:

1. `bot_text` unparseable → `UNCHECKABLE` ("this repository's own artifact is broken").
2. `consumer_text is None` → `LAGGING`, headline "has not vendored the contract yet" (Decision 3).
3. `consumer_text` unparseable → `LAGGING` ("the vendored copy is not valid JSON"). A consumer problem, not a tooling one — an `actions/checkout` tree cannot contain an error page.
4. Byte-identical → `IN_SYNC`.
5. `contract_version` differs → `LAGGING`, headline names the version gap **first and loudest**; the field-level diff is still listed but explicitly caveated, since across a shape change it is not field-comparable.
6. Otherwise → `LAGGING` with `differences()`. If that list is *empty* (bytes differ, semantics don't) the headline says so — formatting/ordering drift still breaks the consumer's byte-identity parity test (§6.1), so it is real lag.

Detail lines are capped (say 50) with a `... and N more` tail, so a version bump produces a readable summary rather than 500 lines.

- [ ] **Step 1: Write the failing tests** — `tests/test_consumer_contract_check.py`

Build fixtures with a tiny `_contract(**overrides)` helper producing minimal valid contract dicts, plus `_text(d) = json.dumps(d, indent=2) + "\n"`. Cover:

```
test_identical_text_is_in_sync
test_an_added_env_var_is_reported_by_name
test_a_changed_placement_names_both_the_old_and_the_new_value
test_a_renamed_env_var_reports_both_the_addition_and_the_removal
test_a_changed_backfilled_default_names_the_column_and_both_values
test_an_added_backfilled_column_is_keyed_by_column_name_not_list_index
test_a_changed_provisioner_required_list_is_reported_as_one_ordered_leaf
test_a_contract_version_mismatch_leads_the_report
test_a_missing_vendored_copy_is_lagging_not_an_error   # Decision 3 / Stage-3 independence
test_a_malformed_vendored_copy_is_lagging_not_uncheckable
test_a_malformed_bot_contract_is_uncheckable
test_byte_drift_with_no_semantic_difference_is_still_lagging
test_the_pin_never_changes_the_verdict                 # Decision 2 -- the key correctness test
test_the_report_is_markdown_and_names_the_consumer_repo
test_the_report_tells_the_reader_which_command_closes_the_lag
test_exit_codes_map_one_to_one_onto_the_three_verdicts
test_check_consumer_contract_does_not_import_the_settings_instance   # AST guard
test_the_real_committed_contract_compares_clean_against_itself       # end-to-end sanity
```

`test_the_pin_never_changes_the_verdict` is the one to get right: pass byte-identical contracts with a wildly stale `pin=` and assert `IN_SYNC`, then pass differing contracts with `pin=None` and assert `LAGGING`. Reversing this produces a permanently red job.

The AST guard mirrors `test_provisioning_contract.py::test_gen_contract_module_does_not_import_the_settings_instance` verbatim (parsed with `ast`, not grepped — a grep matches the module's own docstring explaining the rule, a false positive this project has already hit).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_consumer_contract_check.py -v`
Expected: collection error — `ImportError: cannot import name 'check_consumer_contract' from 'scripts'`.

- [ ] **Step 3: Write the module**

Module docstring must state, in this order: (a) this is **advisory only** and must never gate a push — §6.2; (b) the pin is report context, never a verdict input — Decision 2; (c) a missing vendored copy is `LAGGING`, because Stage 4 does not depend on Stage 3 — Decision 3; (d) the contract carries no secret material by construction (§5.3), which is why the full diff may be printed into a public log, and that nothing *else* may be.

The flatten core:

```python
def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    """Every leaf of the contract, addressed by a dotted path.

    A list of dicts each carrying a `column` key is keyed BY COLUMN NAME,
    not by index: inserting a column into runtime_config.bot_backfilled
    otherwise shifts every later entry and reports one added column as a
    dozen changed ones. Everything else -- including lists of plain
    strings like provisioner_required, whose ORDER is load-bearing -- is a
    leaf compared whole.
    """
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(node, list) and node and all(
        isinstance(item, dict) and "column" in item for item in node
    ):
        out = {}
        for item in node:
            rest = {k: v for k, v in item.items() if k != "column"}
            out.update(_flatten(rest, f"{prefix}[{item['column']}]"))
        return out
    return {prefix: node}
```

`differences()` then emits three sorted groups — paths only the bot has ("missing from the consumer's copy"), paths only the consumer has ("stale; this repo no longer publishes it"), and shared paths whose values differ ("consumer has X, this repo publishes Y") — rendering values with `json.dumps` and truncating long ones.

`render_report()` produces:

```markdown
## Consumer contract lag — TovTechOrg/onboarding-wizard @ main

**LAGGING** — the consumer's vendored contract is 4 fields behind this repository's.

| | |
|---|---|
| This repo's generated contract | `contract_version` 1 · sha256 `a1b2c3d4` |
| Consumer's vendored copy | `contract_version` 1 · sha256 `9f8e7d6c` |
| Consumer's pin (`.ci/pr-review-bot-ref`) | `abc1234` — 42 commits behind `main`, 2026-08-30 |

### What the consumer has not picked up
- `env_vars.GITHUB_TRACKED_REPOS.placement` is missing from the consumer's copy (this repo publishes `"always_synced"`)
- `env_vars.GITHUB_TARGET_REPO.placement` is stale in the consumer's copy — this repo no longer publishes it

### What closes this
In the **consumer** repository:

    uv run python scripts/update_bot_contract.py

then commit `contracts/provisioning.json` and `.ci/pr-review-bot-ref` together.

---
_Advisory only. This job is not attached to any push or pull-request trigger and
gates nothing in this repository — see spec §6.2._
```

That closing line matters: it is what a person seeing a red run reads first.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_consumer_contract_check.py -v`

- [ ] **Step 5: Full suite, lint, and a per-task code review**

Run: `uv run pytest -v && uv run ruff check .` — all green; this task adds two files and modifies none.
Then run the `code-review` skill against this task's diff (see Global Constraints).

- [ ] **Step 6: Commit**

```bash
git add scripts/check_consumer_contract.py tests/test_consumer_contract_check.py
git commit -m "Compare a consumer's vendored provisioning contract against this repo's"
```

---

### Task 2: The CLI — reading the checkout, the summary, the exit codes

Task 1's core takes text. This task adds everything that touches the world: locating the two files, generating the bot's current contract in-process, resolving the pin's context via `git`, writing the step summary, emitting annotations, and returning the exit code.

**Design point worth stating:** the bot side is generated **in-process** via `gen_contract.render()`, not read off disk. §6.2 says "the bot's current generated contract", and regenerating removes any doubt about a stale committed file. The committed `contracts/provisioning.json` is then cross-checked against it — and a mismatch is `UNCHECKABLE`, not `LAGGING`, with the message that `ci.yml`'s `docs` job should have caught it. That is a free extra signal: it detects a stale `main`.

**Files:**
- Modify: `scripts/check_consumer_contract.py` (append)
- Modify: `tests/test_consumer_contract_check.py` (append)

**Interfaces produced:**
- `load_consumer(root: Path) -> tuple[str | None, str | None, str | None]` — `(contract_text, pin_sha, unreachable_reason)`. **`root` missing entirely → `unreachable_reason` set → `UNCHECKABLE`** (the checkout failed: repo private, renamed, or deleted). **`root` present but the contract file absent → `(None, ..., None)` → `LAGGING`** (not vendored yet). Keeping those two apart is why the CLI takes a `--consumer-root` rather than a file path.
- `pin_context(sha: str, repo_root: Path) -> str | None`
- `main(argv: list[str] | None = None) -> int`

CLI surface:

```
--consumer-root PATH     required; the actions/checkout `path:` for the sibling
--bot-contract PATH      optional override; default is gen_contract.render() in-process
--committed-contract PATH  default contracts/provisioning.json; mismatch -> UNCHECKABLE
--summary PATH           default $GITHUB_STEP_SUMMARY when set, else stdout only
--never-fail             always return 0 -- the one-flag switch to Decision 1's option (A)
```

`pin_context()` details that matter:

- **Validate the sha against `^[0-9a-f]{40}$` before it reaches `subprocess`** — that is §5.5's pinned ref format, and validating first (with a fixed argv list, `shell=False`) means a hostile or corrupt pin file can never become a command. A non-conforming pin is reported as "unparseable pin", not shelled out.
- Strip `#`-prefixed comment lines and blank lines first (§5.5 permits them, so a deliberately-held pin can record why).
- Three fixed-argv `git` calls in `try/except`: `merge-base --is-ancestor <sha> HEAD`, `rev-list --count <sha>..HEAD`, `show -s --format=%cs <sha>`. **Any failure returns `None`, never raises.**
- The pin naming a commit *not reachable from `main`* is a real case, not a hypothetical — §5.5 warns that a squash-merged branch tip becomes an "undangling-able pin". Report it as its own line ("the consumer's pin names a commit not reachable from this repository's `main`"), which is actionable, and **still not a verdict input**.

`main()`'s body is wrapped so that any unexpected exception becomes an `UNCHECKABLE` report, a written summary, and exit 2 (Global Constraints).

- [ ] **Step 1: Write the failing tests** (append)

```
test_a_missing_consumer_root_is_uncheckable                  # checkout failed
test_a_consumer_root_without_a_vendored_contract_is_lagging  # Stage 3 not landed
test_an_identical_vendored_contract_exits_zero
test_never_fail_returns_zero_for_every_verdict
test_a_stale_committed_contract_in_this_repo_is_uncheckable
test_main_writes_markdown_to_the_summary_path
test_main_emits_a_notice_a_warning_or_an_error_annotation_per_verdict
test_main_never_raises_on_garbage_input                      # dir as --bot-contract, etc.
test_a_pin_that_is_not_forty_hex_characters_is_never_shelled_out
test_a_pin_file_with_comment_lines_still_yields_the_sha       # spec 5.5 format
test_pin_context_returns_none_when_git_fails
test_the_pin_is_absent_from_the_verdict_but_present_in_the_report
```

For `test_a_pin_that_is_not_forty_hex_characters_is_never_shelled_out`, monkeypatch `subprocess.run` to a sentinel that fails the test if called, then feed a pin like `main; echo pwned`.

- [ ] **Step 2: Run to verify they fail** — `AttributeError: module ... has no attribute 'main'`.

- [ ] **Step 3: Implement `load_consumer`, `pin_context`, `main`**

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Exercise the CLI by hand against the real consumer repo**

Not a test — one real observation that the whole path works end to end before any CI depends on it. Read-only, one shallow clone into the scratch directory, no repo mutation:

```bash
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/TovTechOrg/onboarding-wizard \
  "$SCRATCH/consumer"
git -C "$SCRATCH/consumer" sparse-checkout set --no-cone \
  contracts/provisioning.json .ci/pr-review-bot-ref

uv run python -m scripts.check_consumer_contract \
  --consumer-root "$SCRATCH/consumer" ; echo "exit=$?"
```

Expected **today**, with Stage 3 unlanded: `exit=1`, verdict `LAGGING`, headline "has not vendored the contract yet". That is the correct answer, and observing it is the proof that Stage 4 does not depend on Stage 3.

If instead the clone fails, **stop and report** — that means one of the repos is not public, which invalidates the whole no-token premise of §6.2 and needs a decision, not a workaround.

- [ ] **Step 6: Full suite, lint, code review, commit**

```bash
git add scripts/check_consumer_contract.py tests/test_consumer_contract_check.py
git commit -m "Report a lagging consumer to the step summary with a three-way exit code"
```

---

### Task 3: The scheduled workflow

**Files:**
- Create: `.github/workflows/consumer-contract-lag.yml`
- Create: `tests/test_advisory_workflow.py`
- **Do not touch `.github/workflows/ci.yml`.**

Note in passing: `render.yaml`'s `buildFilter.ignoredPaths` is `**/*.md` only, so landing a new `.github/workflows/*.yml` on `main` triggers one Render redeploy. Per `render.yaml`'s own comment that is the deliberate cost of an ignore-list ("one avoidable redeploy, never a real change silently failing to deploy"). **Do not add `.github/**` to `ignoredPaths` here** — it is a change to deploy-triggering behaviour, needs its own reasoning, and is out of Stage 4's scope. Log it as a Parked Issue if it bothers anyone.

The workflow, with the comments that are the actual deliverable:

```yaml
name: Consumer contract lag (advisory)

# ADVISORY ONLY -- this workflow must never gate a push, a pull request, or a
# deploy (spec section 6.2: "a bot push must not be gated on a landed wizard
# commit"). The mechanism is the trigger list below: `schedule` and
# `workflow_dispatch`, and nothing else. Because it never runs on a commit or
# a PR it can never produce a check run against one, which in turn means it
# cannot be selected as a required status check in branch protection -- the
# property is structural, not a convention someone has to remember.
#
# The BLOCKING half of the contract lives entirely in ci.yml's `docs` job and
# is purely local: no sibling checkout, no pin, no network (spec 5.4).
# tests/test_ci_workflow.py asserts ci.yml never even names the consumer
# repository, which is why this job is a separate file rather than a job there.
#
# A RED RUN HERE MEANS THE CONSUMER HAS NOT CAUGHT UP YET. That is a normal,
# transient state between a contract change landing here and the consumer's
# `update_bot_contract.py` run landing there. It is NOT covered by CLAUDE.md's
# "never push with a red suite" rule, which is about `pytest`/`ruff` and the
# blocking CI job. Exit 2 -- not 1 -- is the "this check could not run at all"
# signal worth actual alarm.
on:
  schedule:
    # Daily. The lag this reports is closed by a human running a script in the
    # other repository, so an hourly cadence would multiply the noise without
    # shortening that response, while a weekly one would let a contract change
    # sit unpropagated for up to seven days -- exactly the window in which a
    # freshly provisioned deployment is misconfigured (the 2026-09-09 incident's
    # shape). Actions minutes are free on public repositories, so daily costs
    # nothing.
    #
    # :17 rather than :00 because GitHub's scheduler is heavily oversubscribed
    # on the hour and delays runs queued there.
    #
    # GitHub disables scheduled workflows in a repository with no activity for
    # 60 days. If this silently stops running, check that first.
    - cron: "17 6 * * *"
  workflow_dispatch:

# Least privilege: this job reads two repositories and writes a step summary.
# It needs no write scope at all. (If Decision 1's option (B) -- filing a
# tracking issue -- is ever adopted, this is where `issues: write` would go.)
permissions:
  contents: read

concurrency:
  group: consumer-contract-lag
  cancel-in-progress: true

jobs:
  consumer-contract-lag:
    runs-on: ubuntu-latest
    steps:
      - name: Check out this repository
        uses: actions/checkout@v7
        with:
          # Full history so the consumer's pinned bot sha can be measured
          # against main. ~8 MiB and 862 commits -- the cost is negligible.
          # The pin is REPORT CONTEXT ONLY and never affects the verdict: a
          # pin can be forty commits old and still perfectly in sync if none
          # of those commits touched the contract.
          fetch-depth: 0

      # NO `token:` HERE, DELIBERATELY. Both repositories are public, so
      # actions/checkout reads the sibling anonymously.
      #
      # THE DAY EITHER REPOSITORY GOES PRIVATE, THIS STEP BREAKS -- and adding
      # a token is not a one-line fix. This workflow's automatic GITHUB_TOKEN
      # is scoped to THIS repository only and grants nothing on the sibling, so
      # a private sibling would need a PAT or a GitHub App installation token:
      # a new secret, with everything CLAUDE.md's secret-handling section
      # implies. Treat a sudden auth failure here as that event, not a flake.
      #
      # continue-on-error so an unreachable sibling still produces a written
      # report (verdict UNCHECKABLE, exit 2) rather than an opaque action
      # failure with an empty step summary.
      - name: Check out the consumer's vendored copy
        id: consumer
        continue-on-error: true
        uses: actions/checkout@v7
        with:
          repository: TovTechOrg/onboarding-wizard
          ref: main
          path: consumer
          persist-credentials: false
          sparse-checkout: |
            contracts/provisioning.json
            .ci/pr-review-bot-ref
          sparse-checkout-cone-mode: false

      - name: Install uv
        uses: astral-sh/setup-uv@v10.0.1

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --all-extras --dev

      # No Postgres service: gen_contract reads class metadata and module
      # constants only, never the database and never a configured value --
      # the same constraint ci.yml's docs job documents.
      #
      # Exit codes: 0 in sync, 1 the consumer is lagging (expected and
      # transient), 2 the check could not be performed. The full report is
      # written to the step summary in all three cases.
      - name: Compare the consumer's vendored contract against this repo's
        run: |
          uv run python -m scripts.check_consumer_contract \
            --consumer-root consumer \
            --summary "$GITHUB_STEP_SUMMARY"
```

`tests/test_advisory_workflow.py`:

**Gotcha to get right:** PyYAML parses a bare `on:` key under YAML 1.1 as the boolean `True`, so `workflow["on"]` is a `KeyError`. Use a helper: `wf.get("on", wf.get(True))`. `tests/test_ci_workflow.py` never reads the trigger block, so there is no precedent to copy — do not assume one.

```
test_the_advisory_workflow_is_not_attached_to_any_code_trigger
    # triggers == {"schedule", "workflow_dispatch"} exactly;
    # and explicitly: "push", "pull_request", "workflow_run" all absent.
    # This is the single test that pins spec 6.2's "never blocking".

test_it_runs_daily_at_an_off_peak_minute
    # exactly one cron; day-of-month, month and day-of-week are all "*";
    # minute is not 0.

test_it_checks_out_the_consumer_repository_the_script_names
    # the checkout step's `repository` == check_consumer_contract.CONSUMER_REPO
    # and its `ref` == "main" -- so the workflow and the module cannot drift.

test_the_consumer_checkout_passes_no_token
    # no `token` key on that step. Pins the public-repos-need-no-token premise
    # so that the day it stops being true, this fails loudly here too.

test_the_workflow_documents_what_happens_when_a_repo_goes_private
    # the raw text mentions "public" and "private" near that step -- spec 6.2
    # explicitly asks for this comment, so it is asserted rather than trusted.

test_the_consumer_checkout_tolerates_an_unreachable_sibling
    # continue-on-error is true on that step, so an unreachable consumer still
    # produces a written report rather than an empty summary.

test_the_workflow_only_asks_for_read_permission
    # permissions == {"contents": "read"}.

test_the_compare_step_invokes_the_module_the_unit_tests_cover
    # "scripts.check_consumer_contract" appears in the run commands, and the
    # --consumer-root argument matches the checkout step's `path:`.

test_the_blocking_workflow_is_untouched_and_still_names_no_sibling
    # ci.yml contains neither "onboarding-wizard" nor "workflow_run" -- the
    # belt-and-braces companion to tests/test_ci_workflow.py's own assertion.
```

- [ ] **Step 1: Write the failing tests**
- [ ] **Step 2: Run to verify they fail** — `FileNotFoundError` on the workflow path.
- [ ] **Step 3: Add the workflow file**
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Validate the YAML actually parses as a workflow**

Run: `uv run python -c "import yaml,pathlib;print(sorted(yaml.safe_load(pathlib.Path('.github/workflows/consumer-contract-lag.yml').read_text())))"`
Expected: the top-level keys, with the trigger block appearing as `True` — confirming the parser gotcha the tests handle.

- [ ] **Step 6: Full suite, lint, code review, commit**

```bash
git add .github/workflows/consumer-contract-lag.yml tests/test_advisory_workflow.py
git commit -m "Report a lagging contract consumer daily, never blocking a push"
```

---

### Task 4: Validate the failure path, not just the plumbing

§10's closing paragraph: *"on a throwaway branch, add a column to `RUNTIME_CONFIG_COLUMNS` and confirm ... (c) the advisory job reports the wizard as lagging. Then discard the branch. The superseded design's rollout proved only that the plumbing ran on an unchanged schema."*

Adapted to Stage 4's slice, and split by risk. Do **4a**; ask before doing **4b**.

**A constraint that shapes this, and must not be discovered the hard way:** GitHub only offers `workflow_dispatch` for a workflow file that exists **on the default branch**, and `schedule` fires **only** on the default branch. So the workflow cannot be exercised live before this branch merges. That is fine, and is itself an argument for the design: the job is non-blocking by construction, so landing it unexercised risks nothing that a red run would.

- [ ] **Step 4a: The local drill — mutate nothing in this repo**

The point of §10's throwaway column is to prove the detector fires on a *real* difference. That can be proven without touching `RUNTIME_CONFIG_COLUMNS` at all, by simulating the stale side instead of the fresh one — same detection path, zero repo mutation, no red CI run, no throwaway branch:

1. Fetch the real consumer copy into the scratch directory (Task 2 Step 5's clone).
2. **Drill 1 — not yet vendored.** Run against the real tree as-is. Expect `exit=1`, headline "has not vendored the contract yet". *(Once Stage 3 lands this drill instead needs an empty scratch directory to reproduce.)*
3. **Drill 2 — a genuinely stale copy.** Copy this repo's `contracts/provisioning.json` into the scratch consumer tree, then edit **the scratch copy** to look like a consumer one contract change behind — e.g. rename an `env_vars` key and change one `bot_backfilled` default. Run. Expect `exit=1` and detail lines naming *exactly* the renamed key (added **and** removed) and the changed default with both values.
4. **Drill 3 — a corrupt copy.** Truncate the scratch copy mid-JSON. Expect `exit=1` (`LAGGING`, "not valid JSON") — **not** exit 2.
5. **Drill 4 — an unreachable consumer.** Point `--consumer-root` at a nonexistent directory. Expect `exit=2`, `UNCHECKABLE`.
6. **Drill 5 — in sync.** Copy this repo's contract into the scratch tree unmodified. Expect `exit=0`, `IN_SYNC`.
7. Confirm `git status` in the real repo is clean throughout, and delete the scratch tree.

Record the five observed exit codes and headlines in the task report. **Write that report after running the drills, not in advance** — per `CLAUDE.md`, documentation describing a live-verification outcome is written from the actual outcome, never drafted assuming success.

- [ ] **Step 4b: The live drill — only after Stage 4 is on `main`, and only with the user's explicit OK**

This is §10's literal version and it costs one deliberately-red CI run on a throwaway branch, which sits in tension with `CLAUDE.md`'s "never push with a red suite". **Do not do it on your own initiative — ask first, naming that tension.**

If approved, after the merge to `main`:

1. `git switch -c drill/contract-lag-check` from `main`.
2. Add one obviously-fake column to `store.RUNTIME_CONFIG_COLUMNS` (`drill_only_column DOUBLE PRECISION`) and regenerate: `uv run python -m scripts.gen_contract`.
3. Push the branch. **The branch only** — never `main`. Render deploys from `main`, so a branch push deploys nothing.
4. Observe §10's (b): `ci.yml`'s `docs` job goes red on the freshness gate if the contract was *not* regenerated, and green once it was. Both halves are the gate working.
5. Observe §10's (c): `gh workflow run consumer-contract-lag.yml --ref drill/contract-lag-check`, then read the run's step summary. It must report `LAGGING` and **name `drill_only_column` explicitly** in the detail lines.
6. Confirm on the same run page that the advisory workflow produced **no check run against any commit or PR**, and that no `ci.yml` job waited on it.
7. Delete the branch locally and on the remote. Confirm `git log --oneline origin/main -3` shows no trace, and that `main`'s own advisory run is back to its pre-drill verdict.

- [ ] **Step 5: Post-merge sanity, once `main` has it**

- [ ] Confirm the workflow appears under the repo's Actions tab with a "Run workflow" button (proves the `workflow_dispatch` trigger registered on the default branch).
- [ ] Trigger one manual run and read the step summary end to end — this is the first time the *real* checkout of the sibling runs inside Actions rather than locally.
- [ ] Confirm in the repo's branch-protection settings that the advisory job is **not** listed as a required status check, and cannot be selected (it produces no check runs). This is the manual half of the "never blocking" guarantee the test suite cannot reach.
- [ ] Confirm the run appears on the Actions tab and **not** as a badge on the README (deliberately not added — see Decision 1).

---

## Done criteria

- [ ] `uv run pytest -v` fully green; `uv run ruff check .` clean.
- [ ] `scripts/check_consumer_contract.py` compares two contract texts with no network, no `git`, and no filesystem in its core, and never imports `config.settings` (pinned by AST).
- [ ] The verdict is driven **only** by the contract comparison. The consumer's pin appears in the report and nowhere in the decision.
- [ ] A consumer with **no** vendored contract reports `LAGGING` with a clear headline — so Stage 4 stands alone without Stage 3.
- [ ] A malformed vendored copy is `LAGGING`; an unreachable sibling or a stale committed contract in *this* repo is `UNCHECKABLE`. The two are never conflated.
- [ ] The script never tracebacks out: an unexpected exception is reported as `UNCHECKABLE` with a written summary and exit 2.
- [ ] `.github/workflows/consumer-contract-lag.yml` triggers on `schedule` + `workflow_dispatch` only — no `push`, no `pull_request`, no `workflow_run` — asks for `contents: read` only, passes no token to the sibling checkout, and carries the public/private comment §6.2 asks for.
- [ ] `.github/workflows/ci.yml` is byte-for-byte unchanged, and `tests/test_ci_workflow.py` still passes unmodified.
- [ ] Step 4a's five drills were run and their **actual** observed outcomes recorded.
- [ ] **Not done here, deliberately:** no push (`CLAUDE.md` requires `deploy-verify` before any push to `main`); nothing in the `onboarding-wizard` repository (Stage 3); no `CLAUDE.md` section and no deletion of the superseded spec (Stage 5 — §9/§10); no README badge; no change to `render.yaml`'s `buildFilter`; Step 4b only if the user explicitly approves it.

---

## Summary of key decisions

- **Separate workflow file, not a job in `ci.yml`** — and this is already mechanically enforced: `tests/test_ci_workflow.py::test_the_contract_freshness_check_needs_no_database_or_sibling_checkout` asserts the string `onboarding-wizard` appears nowhere in `ci.yml`. Stage 2 left that trap deliberately.
- **`actions/checkout` (sparse, non-cone, no token)** over `raw.githubusercontent.com` or the Contents API. The decisive reason is not convenience: a raw fetch's 404-as-HTML makes "not vendored yet", "went private", and "renamed" indistinguishable, collapsing the `LAGGING`/`UNCHECKABLE` split the whole report depends on. Checkout also picks up `.ci/pr-review-bot-ref` for free and fails unmistakably the day either repo goes private — the event §6.2 wants commented.
- **Reporting (the design's gap, flagged for you):** full markdown to `$GITHUB_STEP_SUMMARY` + workflow annotations, with exit 0/1/2 for in-sync/lagging/uncheckable, so a lagging consumer turns the *scheduled* run red and GitHub's built-in scheduled-failure email does the notifying — no token, no secret, no issue-spam dedup. Alternatives (warn-only; a deduped tracking issue needing `issues: write`; Slack needing a new secret) are written up with a `--never-fail` flag shipped so option (A) is a one-word switch. The known cost — persistent red reading as normal during a legitimate lag window — is stated with its mitigations.
- **The pin is context, never a verdict input.** An old pin is not lag; only a differing contract is. Reversing this yields a permanently red job, so it gets its own test.
- **A missing vendored copy is `LAGGING`, not an error** — which is exactly what makes Stage 4 independent of Stage 3, and is observable today via Task 2 Step 5.
- **Never blocking is structural, not conventional:** with no `push`/`pull_request` trigger the job cannot produce a check run against a commit or PR, so it cannot even be *selected* as a required status check.
- **Test coverage splits cleanly:** the comparison logic gets real unit tests (pure functions over strings); the workflow gets structural YAML assertions in a new `tests/test_advisory_workflow.py` (watch the PyYAML `on:` → `True` gotcha); the trigger itself is verified manually post-merge, since `workflow_dispatch` and `schedule` only register from the default branch.

### Critical Files for Implementation
- `/home/emanresu/pr-review-bot/scripts/gen_contract.py`
- `/home/emanresu/pr-review-bot/.github/workflows/ci.yml`
- `/home/emanresu/pr-review-bot/tests/test_ci_workflow.py`
- `/home/emanresu/pr-review-bot/contracts/provisioning.json`
- `/home/emanresu/pr-review-bot/tests/test_provisioning_contract.py`
