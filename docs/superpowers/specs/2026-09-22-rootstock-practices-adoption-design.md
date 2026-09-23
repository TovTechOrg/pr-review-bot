# Adopting selected Rootstock-OS practices across both repos (2026-09-22)

Five procedural practices distilled from the Rootstock-OS Claude Code
workflow kit, adopted on their own merits and without the framework they
came from, plus two cleanups the evaluation surfaced: relocating
machine-local knowledge out of the repos, and adding a navigable index to
the one class of file whose whole-file reads are genuinely expensive.

Scope is both `pr-review-bot` and `onboarding-wizard`. This spec is
authoritative for both; the wizard carries no copy of it.

## What this is for

`CLAUDE.md` is the only project markdown loaded into every session before
any work begins. It has grown to 26,782 bytes in this repo and 34,600 in
the wizard, and nothing mechanical notices when it grows further. Both
repos already practise the fix by hand -- state a rule in one line, push the
elaboration into `docs/conventions/rationale.md` -- but doing it depends on
someone noticing. The same is true of several process rules that currently
exist only as habits.

Everything here makes an existing convention mechanical, or writes down a
rule that was being re-derived per session. Nothing here introduces a new
framework, a new vocabulary, or a new place to look things up.

## What was evaluated and rejected

Rootstock-OS (`Mazhron/rootstock-os`) is a solo-author, roughly three-week-old
kit bundling a fine-grained wiki structure, scripted reporting, subagent
conventions, and a stack of `PreToolUse`/`PostToolUse` hooks. It was
evaluated as a candidate integration and rejected wholesale. Recorded here
so the evaluation is not repeated:

- **Its token-savings premise is oversold.** Its headline numbers come from
  repairing a pathological baseline -- 800k+ token re-reads per request from
  runaway sessions. Prompt caching already discounts repeated reads of a
  stable file heavily, and splitting knowledge across roughly 47 files
  imposes a fragmentation tax (missed sections, extra round-trips) its README
  does not account for.
- **Its hook layer is unvetted third-party code** intercepting every
  `Bash`/`Write`/`Edit` call. These repos already require
  `.claude/hooks/` to stay byte-identical across both checkouts, and the
  existing hooks (`check_env_access.py`, `redact_output.py`) are the
  enforcement mechanism behind this project's highest-priority rules. A
  second, independently maintained hook stack is not worth the review burden.

What survives is procedural only: five ideas that stand alone, cost little,
and fit conventions both repos already have.

## The premise correction this design rests on

The original framing was that a front-matter refactor across the repos'
markdown would reduce **upfront** token usage. It does not, and the design
would have been built on a false floor without saying so.

Only three things are loaded into a session before any tool call: the global
`CLAUDE.md`, the project `CLAUDE.md`, and the memory index. Neither repo uses
`@`-imports, so nothing else is pulled in automatically. `ISSUES.md`,
`docs/conventions/rationale.md`, and every spec and plan are read on demand,
by explicit tool call. Their upfront cost is already zero, and adding YAML to
them adds bytes to every read while removing nothing from the preamble. For a
file that is always read whole, front-matter is a small net loss.

The real cost is **retrieval**, not preamble. `ISSUES.md` is 105,035 bytes
here and 93,144 in the wizard -- roughly 26k and 23k tokens in a single tool
result. Plan files run 50-100 KB each. Front-matter pays only where it
converts a whole-file read into a targeted one.

Two consequences run through the rest of this spec. First, the
`CLAUDE.md` budget (item 1) is the genuine upfront-token lever, and the only
one. Second, the front-matter work is scoped by read pattern rather than
applied uniformly, and is justified on retrieval cost alone.

## Item 1 -- a CLAUDE.md budget, enforced as a repo-property test

### Mechanism

`tests/test_claude_md_budget.py` in each repo, joining the existing
repo-property meta-tests (`test_dockerfile.py`, `test_ci_workflow.py`,
`test_dockerignore.py`). No new workflow and no new pre-commit hook: a test
rides both the blocking `lint-and-test` CI job and the mandatory pre-push
`uv run pytest -q`, which is strictly more coverage than either alternative
for none of the cost.

Three assertions:

1. **The `## Secret handling` heading exists.** Fails loudly if absent, so
   deleting the heading cannot be used to shrink the budgeted region.
2. **Bytes outside the exempt sections are at most 18,000.** A section runs from
   its `##` heading to the next `##` heading, so this repo's nested
   `### Scoped exception: the dashboard Environment tab` stays exempt.
3. **The whole file is at most 32,000 bytes.** This closes the perverse
   escape of relocating prose *into* the exempt section to pass assertion 2.

### Which sections are exempt, and why

`## Secret handling` is 11,029 bytes here and 7,179 in the wizard -- roughly
40% and 21% of each file. It is also the one section that must never be what
gets trimmed to make a test go green. Excluding it from the budget removes
the incentive entirely rather than relying on an agent to resist it.

**The exemption principle, so that "exempt" does not drift into "important
to me":** a section is budget-exempt only if it governs handling of
credentials or secrets -- the operator's or a visitor's -- **and** trimming
it to satisfy a byte budget would be a safety regression. Nothing else in
either file qualifies today.

**The wizard has a second security domain, and it is currently unprotected.**
Its `## Secret handling` covers the operator's own secrets; the handling of
*visitors'* credentials is scattered across `## Rules`, `## What the
implementation adds to these rules`, and a bullet in the hygiene list. That
scattering is what led an earlier draft of this spec to relocate genuine
leak-prevention content to `rationale.md` -- the `RequestValidationError`
handler that stops FastAPI echoing a submitted credential back in a 422, the
prohibition on a credential reaching `localStorage`, and the
`..._leaves_the_page_exactly_once` credential-exit-path audit. Moving those
behind a link to satisfy a byte budget is exactly the failure the exemption
exists to prevent.

These consolidate as `###` subsections **inside** the wizard's existing
`## Secret handling`, not as a second top-level exempt section:

- `### Visitor-supplied credentials: the relay contract` -- the current
  `## Rules` (2,141 B), which already describes itself as an extension of
  the secret-handling section: "same standard as this file's secret-handling
  section applies to the operator's own secrets, applied here to strangers'
  secrets, which if anything deserves *more* caution".
- `### What the implementation adds` -- the security-bearing half of
  `## What the implementation adds to these rules` (~2,000 B): the
  `RequestValidationError` handler, the `localStorage` prohibition, and the
  one-exit-path audit. It also absorbs the security half of the
  `replace=True` rule (a plain merge leaves the *previous* account's
  `service_id`/`database_url` in the session, which the completeness check
  then reports as done for the wrong account); the `asyncio.to_thread`
  wrapper requirement stays in the body, being a performance rule.
- `### SSRF: credential-accepting endpoints built from visitor-supplied
  structure` -- the SSRF rule (~1,100 B), relocated out of the hygiene list
  where it sat only because of where it happened to be written down.

Nesting rather than adding a second top-level section is deliberate. The
budget test already exempts everything from a `##` heading to the next `##`,
so nested subsections need **zero test changes** and the test stays
identical across both repos. This repo already sets the precedent with
`### Scoped exception: the dashboard Environment tab`. And one exempt
heading per repo keeps the exemption a single, narrow loophole rather than a
growing category -- the 32,000-byte total cap is the only thing holding that
line.

The failure message is part of this design, not an afterthought. A test that
only prints `26782 > 18000` invites the wrong fix. It must name
`docs/conventions/rationale.md` (and, in the wizard, `docs/subprojects/`) as
the overflow destination, and state that trimming the secret-handling
section is never the correct response.

### Why 18,000

Measured, not chosen for roundness. Current budgeted remainders are 15,753
bytes here and 27,421 in the wizard. After the exemption above, the
relocations below, and the additions in item 2 and item 3, this repo lands at
roughly 15.4 KB and the wizard at roughly 14.3 KB -- leaving about 2.6 KB and
3.7 KB of headroom respectively.

**This repo is the binding constraint, not the wizard.** That is a reversal:
20,000 was chosen in an earlier draft precisely because 18,000 would have left
the wizard under 1 KB of headroom. Carving the wizard's visitor-credential
content into the exempt section removes ~5.2 KB from its budgeted remainder,
which makes the tighter ceiling affordable. A budget that trips on the next
rule anyone adds trains people to raise the budget rather than relocate
content, which defeats it -- 2.6 KB is the smallest headroom that still
clears that bar.

Totals stay well inside the 32,000-byte cap: ~26.5 KB here and ~26.7 KB in
the wizard, roughly 5.5 KB of slack each.

### The wizard's relocation work

The wizard must shed roughly 7.9 KB from its budgeted remainder, on top of
the ~5.2 KB that moves into the exempt section above. Destinations were verified against the
file's actual contents, and the wizard already documents the pattern in its
own `## Subsystem-specific rules (routing table)`: subsystem rule sets are
"moved out of this file so it only loads when actually relevant", carrying
incident history "at the same priority as anything kept inline here, just not
loaded by default."

| Section | Size | Action | Freed |
|---|---|---|---|
| `## The cross-repo contract with the review-engine project` | 4.0 KB | Split to `rationale.md`, exactly as this repo already does (this repo keeps ~1.5 KB inline under `### Cross-repo contract direction`, detail under `## Cross-repo contract direction (2026-09-10)`; the wizard's `rationale.md` has no such section yet) | ~2.8 KB |
| `## The invariant this service now protects` | 4.9 KB | New `docs/subprojects/session-store.md` plus a routing-table entry; the `_get_session`/`_read_frame`/`_update_frame` wrapper requirement stays as an inline stub, `replace=True`'s security half moves into the exempt section | ~4.0 KB |
| `## What the implementation adds to these rules` | 3.6 KB | Security-bearing half (~2.0 KB) into the exempt section; the rest to `rationale.md` | ~1.1 KB |
| `## Plan-execution / multi-agent process hygiene` | 2.6 KB | SSRF rule (~1.1 KB) into the exempt section; remainder collapsed per item 4 | ~0.8 KB |
| `## Hebrew strings never chain multiple embedded LTR terms with an arrow` | 1.1 KB | Compress to a pointer; `rationale.md` already holds the full detail | ~0.7 KB |
| `## Workspace isolation: worktree vs inline` | 1.6 KB | Harness half moves to global (see "Relocations") | ~0.7 KB |
| **`## Rules`** | 2.1 KB | Becomes `### Visitor-supplied credentials: the relay contract` inside `## Secret handling`. Content unchanged; it leaves the budget by becoming exempt, not by being trimmed | ~2.1 KB |

This repo needs no relocation to pass; it starts at 15,753 bytes.

## Item 2 -- the Contradiction Rule

Placed in **both projects' `CLAUDE.md`**, in the `## Conventions` section.

When a request conflicts with a rule that is already established, name the
conflict and get explicit confirmation before proceeding. Never silently
comply; never silently pick a side.

Two constraints keep it from becoming an asking tax:

- **It fires only on a rule that is written down** -- a `CLAUDE.md`, a
  committed spec under `docs/superpowers/specs/`, a memory file, or a
  recorded decision in `ISSUES.md`. A conflict with an unwritten preference
  or a stylistic nicety gets a judgment call and a one-line mention, not a
  block.
- **It fires only on a material conflict** -- one where proceeding under
  either reading produces work that is wrong under the other. This is the
  narrow case where a blocking question is warranted.

Required response: name the rule and where it is written, state both
readings, recommend one, stop.

The rule closes properly. A reaffirmed request is the decision: proceed with
the full request, and record the resolution as a `feedback` memory so the
same contradiction does not have to be re-litigated next session.

## Item 3 -- a cheap probe before escalating

Placed in **both projects' `CLAUDE.md`**, in the `## Conventions` section.

Before dispatching a subagent that will read many files or run a broad
review, state in one line which cheap probe was already run -- a grep, a
glob, a single targeted read -- and why it was insufficient. If none was
run, run one first.

An explicit carve-out keeps genuine fan-out untaxed: "breadth unknown, a
grep would need N guesses" is a complete and acceptable answer. The rule
exists to stop reflexive escalation, not to litigate every dispatch.

**Justified on token and latency cost only.** The memory-pressure rationale
that originally motivated it is specific to one machine's WSL2 VM and stays
in the global `CLAUDE.md`, with no cross-reference from either repo. The
token argument is repo-agnostic and holds for any contributor on any
machine, which is what makes the rule worth checking into the repos at all.

This is the weakest of the five items, and it is recorded as such. Its value
rests entirely on compliance with prose; the one-line statement requirement
is what gives it an observable artifact rather than leaving it as
exhortation.

## Item 4 -- the lesson entry format, inside rationale.md

**No new file.** A `LESSONS.md` was specified originally and is rejected:
both repos already route between `CLAUDE.md`, `docs/conventions/rationale.md`,
and `ISSUES.md`, and a fourth top-level document overlapping
`rationale.md`'s existing `## Plan-execution / multi-agent process hygiene:
full detail` section would reproduce exactly the fragmentation tax that
disqualified the source kit's wiki.

What is genuinely novel in the Rootstock format is the negative space --
what was tried and why it failed. `rationale.md` records why the chosen way
is right, rarely what was burned reaching it. That is worth adopting as an
entry shape:

```
### <lesson title>

**The one right way:** <single imperative sentence>

- **Tried:** <what was actually attempted>
- **Failed because:** <the mechanism of failure, not the feeling>
- **Do instead:** <the concrete corrective>

**Nuance:** <lines accumulated as the lesson is re-learned>
```

**Sourcing constraint, binding:** `Tried` and `Failed because` must trace to
a real dated incident in that repo's `ISSUES.md`. An entry with no incident
behind it keeps the existing prose form rather than being given a fabricated
failure story. The "don't reconfirm the baseline" entry is the clear case --
it comes from measurement during the 2026-08-19/20 test-suite-performance
work, not from an incident, and must not be dressed as one.

`CLAUDE.md` keeps a pointer plus the highest-stakes one-liners:

**Both repos, the same three:** stop-and-report is a hard stop; run the
`code-review` skill immediately on any task diff touching external-API/auth
integration; every parked finding goes to `ISSUES.md`.

The lists are identical, and that is a change. Measured against the current
files, the two hygiene lists are byte-identical except for a single extra
wizard bullet: the SSRF rule. That rule is a security rule about a
vulnerability class, not a plan-execution process lesson -- it sits in the
hygiene list only because that is where it happened to get written down after
a review. Relocating it into the wizard's exempt security section (see item 1)
removes the asymmetry rather than documenting it.

**Consequence:** with the lists identical, they join the parity-tested shared
sections below. The SSRF bullet diverging silently is precisely the drift that
test exists to catch.

## Item 5 -- which repeatable checks are ledger-eligible

This item was specified as extending "trust the ledger" to `deploy-verify`
and the consumer-contract-lag job. Both fail scrutiny, and the item becomes a
documentation clarification that says no more than yes. One new
`rationale.md` entry, `## Which repeatable checks are ledger-eligible
(2026-09-22)`, pointed at from the existing ledger bullet in `CLAUDE.md` at
near-zero budget cost.

- **Eligible:** the full-suite `pytest`/`ruff` baseline within a plan's
  execution. The existing rule, unchanged: trust the SDD ledger's
  last-recorded green state rather than reconfirming per task.
- **Not eligible -- `deploy-verify`.** It is unconditional before any push
  to `main` by incident (the 2026-09-03 `python-multipart` deploy crash),
  and structurally unfit besides: the image `COPY`s the whole tree, so
  "unchanged version" is essentially never true at push time. A ledger here
  would either never hit or hit wrongly, and weakening the rule would
  reopen a closed incident to buy nothing.
- **Not eligible -- `consumer-contract-lag`.** Schedule-only and advisory.
  There is no manual re-run to prevent, a red run is the normal transient
  state, and gating anything on it inverts the ownership direction the
  cross-repo contract design establishes.

**The wizard's copy drops the lag paragraph entirely** -- it has no such
workflow -- and instead notes that its own blocking CI job carries a pinned
sibling checkout. This is the concrete case where this repo's rationale does
not carry over unchanged, and the reason each repo's entry is written
separately rather than copied.

## Relocations: machine-local knowledge leaves the repos

A scan of both `CLAUDE.md` files for machine- and setup-specific content
found three items. (Searched for and **not** found: any stray toggle,
enable/disable, or fast-mode setup instructions. The single `toggle` match
is the dashboard Environment tab's per-row reveal control, which is product
behaviour.)

**1. `## Impeccable comp-first image generation (manual bridge)`** -- 630
bytes in this repo's `CLAUDE.md` plus a 2.1 KB `rationale.md` entry. Moves
in full to the global `CLAUDE.md`. The entry argues for its own relocation:
the script is "deliberately outside this repo (machine-local tooling, not a
project dependency; no entry in `pyproject.toml`, nothing for other
contributors to install)", and the router path is hand-rolled because
"`huggingface_hub` itself isn't installed in this environment (no working
`pip`)". Both the script and its token live under `~/.config/impeccable-hf/`.
A second reason: the wizard does UI work too and has no such section, so
global placement fixes a gap rather than duplicating one.

**2. `## Workspace isolation: worktree vs inline`** -- splits. The
harness-incompatibility half (the redaction wrapper and `EnterWorktree`'s
isolation guard do not compose; every git command is refused) is byte-
identical in both repos and concerns Claude Code's tooling rather than
either product. It moves to the global `CLAUDE.md` once, removing an
existing duplication. The "Which to use" half stays local in each repo: it
names `ui-visual-review`, `deploy-verify`, and this repo's
`tests/test_config.py` placement guards.

**3. Hardcoded checkout paths.** Both hook-parity bullets name
`~/pr-review-bot` and `~/onboarding-wizard`, encoding one machine's
directory layout into files that ship to anyone who clones. Rephrased as
"the sibling checkout", following the precedent the wizard's own CI sets by
resolving the sibling through `PR_REVIEW_BOT_PATH`.

**One deliberate non-move:** the `PowerShell` mention in both
secret-handling sections looks platform-irrelevant on Linux, but accurately
documents `check_env_access.py`'s real matcher list. Trimming it would
misdescribe the hook. Recorded so it is not raised later as an oversight.

The global `CLAUDE.md` is unversioned and untestable -- item 1's budget test
cannot see it, and nothing enforces or rolls back its contents. That
asymmetry is accepted rather than solved: none of the relocated content had
mechanical enforcement in the repos either.

## Anchor integrity

`CLAUDE.md` already deep-links into `rationale.md`: six anchors here, four
in the wizard. Nothing validates them. `mkdocs build --strict` covers
internal links, but `mkdocs.yml` sets `docs_dir: guide`, so `rationale.md`,
`ISSUES.md`, and everything under `docs/` are outside the build entirely.

This work breaks those links itself. Relocating the Impeccable section
orphans `rationale.md#impeccable-comp-first-image-generation-manual-bridge`;
splitting the workspace-isolation section disturbs
`#workspace-isolation-measurement-detail-and-worktree-caveats`; item 4
renames the hygiene section's anchor.

`tests/test_doc_anchors.py` in each repo asserts:

- every `*.md#anchor` link originating in `CLAUDE.md` resolves to a real
  heading in the target file, with slugs computed the way the existing
  anchors are spelled;
- every `docs/subprojects/*.md` named in the wizard's routing table exists.

Written **before** the relocations, so it goes red against them and drives
the link fixes, matching the repos' test-first convention.

## Front-matter, scoped by read pattern

Two tiers, justified on retrieval cost per the premise correction above.
(The labels A and C are kept from the decision record; what would have been
tier B -- anchor integrity -- is not front-matter at all and has its own
section above.)

**Tier A -- `ISSUES.md` in both repos.** Both files have exactly 57 headings
and an identical two-tier shape: a `## <short title>` template stub, roughly
a dozen `##` incident entries, then `## Parked Issues` holding roughly forty
`###` entries. A `read_index` front-matter block lists, per entry: anchor
slug, date, one-line title, and `parked: true|false`. An agent reads the
index with `head` and then `sed`s a single range instead of pulling 105 KB.

Guarded by `tests/test_issues_index.py`, asserting **bidirectionally** that
every indexed anchor exists in the body *and* every entry heading in the body
appears in the index. One direction alone lets the index rot silently, which
would make it worse than no index.

**Tier C -- new specs and plans, going forward.** Front-matter comes from the
`writing-plans` template. **No retrofit:** roughly fifty historical plan files
at 50-100 KB each are write-once artifacts, and indexing them buys nothing.
Note that no existing spec in either repo carries front-matter, so this is a
new convention for new documents rather than a change to existing ones.

**Skipped deliberately.** `rationale.md` (18,113 bytes here, 13,398 in the
wizard), `docs/subprojects/*.md`, and `guide/*.md` are read whole or already
link-checked by `mkdocs --strict`; front-matter there is a net token loss.

**Excluded hard.** `guide/reference/*.md` is generated by `scripts.gen_docs`,
carries a "do not edit by hand" banner, and has a CI staleness check -- any
front-matter there must be emitted by the generator, never hand-added.
`SKILL.md` and `.claude/commands/*.md` already carry machine-consumed
front-matter and are left alone.

Tier A is the only part with a measurable payoff today. If it is dropped, the
rest of this spec is unaffected.

## Cross-repo parity for the three shared sections

Items 2 and 3, plus the hygiene one-liner list from item 4, are repo-agnostic
prose that must read identically in both repos -- two subtly different
Contradiction Rules would be worse than one, because an agent could not tell
which reading governs. Convention alone is
insufficient here: `CLAUDE.md` already records that the hook-parity
convention failed silently once, when `check_env_access.py` drifted.

Mechanical enforcement is available without new infrastructure. The wizard's
CI already performs a full, non-sparse checkout of this repo at the ref
pinned in `.ci/pr-review-bot-ref`, for `test_bot_contract_parity.py`.
`tests/test_claude_md_shared_sections_parity.py` in the **wizard only**
compares all three shared sections byte-for-byte against that checkout.

The direction matters and follows the established cross-repo contract
ownership: **this repo is the producer and is never blocked on the
consumer.** It gains no parity test. The wizard, as consumer, verifies
itself against the pin.

**This creates a pin-bump dependency where none existed.** The wizard's test
can only pass once `.ci/pr-review-bot-ref` points at a commit here that
contains the new sections. The pin is currently
`6e80b8b336ad46e34f5cd3aca7a3dd82879e8505`, this repo's present `main`.

## Phases

Each phase is independently landable and leaves both repos green. A session
that has to stop can stop at a phase boundary without leaving either repo
half-migrated.

**Phase 1 -- budget, security consolidation, and anchors.** In order:
the wizard's security consolidation first (`## Rules`, the security half of
`## What the implementation adds to these rules`, `replace=True`'s security
half, and the SSRF rule become `###` subsections of `## Secret handling`),
because it changes what the budget test treats as exempt; then
`test_claude_md_budget.py` and `test_doc_anchors.py` in both repos; the
wizard's remaining relocation work; item 4's reformatting and collapse;
item 5's rationale entry; the three relocations to global. No cross-repo
ordering dependency.

**Phase 2 -- shared rules and parity.** Items 2 and 3 into both
`CLAUDE.md` files, then the wizard's parity test over all three shared
sections. **Depends on Phase 1 in the wizard:** the hygiene lists are only
identical once the SSRF rule has moved, so the parity test cannot pass
before that relocation lands. **Hard ordering:** this
repo lands on `main` first, then `.ci/pr-review-bot-ref` is bumped to that
commit, then the wizard lands -- the test reads this repo's `CLAUDE.md`
through the pin, so it needs both prerequisites met.

**Phase 3 -- front-matter.** Tier A in both repos with its bidirectional
test; Tier C as a template change only.

## Testing

Four new test modules -- seven files in total, since three of the four exist
in both repos -- all following the existing repo-property meta-test
convention and requiring no fixtures, no database, and no network:

| Test | Repos | Asserts |
|---|---|---|
| `test_claude_md_budget.py` | both | secret-handling heading present; non-exempt bytes <= 18,000; total bytes <= 32,000 |
| `test_doc_anchors.py` | both | every `CLAUDE.md` anchor link resolves; wizard routing-table targets exist |
| `test_claude_md_shared_sections_parity.py` | wizard | all three shared sections byte-identical to the pinned bot checkout |
| `test_issues_index.py` | both | index and body entries match bidirectionally |

The existing rule applies unchanged: `uv run pytest -q` and
`uv run ruff check .` green before any push, and the `deploy-verify` skill
before any push to `main` in either repo.

## Non-goals

- **Not adopting Rootstock-OS itself** -- not its wiki structure, its hook
  stack, its scripted reporting, or its subagent conventions.
- **Not a `LESSONS.md`.** Rejected in favour of reformatting what exists.
- **Not a deploy-verify ledger.** Explicitly rejected in item 5.
- **Not retrofitting historical plans** with front-matter.
- **Not touching `.claude/hooks/`**, so no byte-parity work is required
  there.
- **Not rewriting the wizard's `## Rules`.** Its content is unchanged; it
  moves under `## Secret handling` as a `###` subsection and becomes exempt.
- **Not altering either repo's existing secret-handling prose.**

## Open verification points

Three things are deliberately left to plan-writing time rather than asserted
here, because asserting them would mean inferring structure that has not
been read:

1. **The `writing-plans` skill's actual template**, before Tier C is shaped
   around it. Front-matter must compose with what the skill really emits.
2. **`scripts/update_bot_contract.py`'s behaviour on a pin-only bump** with
   no contract change. Phase 2 needs the pin moved; whether that script is
   the right instrument, or whether it re-vendors an unchanged contract as a
   harmless side effect, must be read rather than assumed.
3. **The exact slug spelling `test_doc_anchors.py` must compute**, derived
   from the anchors already in use rather than from a general slugify rule.
