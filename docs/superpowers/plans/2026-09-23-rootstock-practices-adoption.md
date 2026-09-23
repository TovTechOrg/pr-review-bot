# Rootstock-OS Practices Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make five procedural conventions mechanical across `pr-review-bot` and `onboarding-wizard` -- a `CLAUDE.md` byte budget, a Contradiction Rule, a cheap-probe-before-escalating rule, a lesson entry format, and a written statement of which repeatable checks are ledger-eligible -- plus an anchor-integrity guard and a navigable index on `ISSUES.md`.

**Architecture:** Every enforceable piece lands as a repo-property meta-test alongside the existing `test_dockerfile.py` / `test_ci_workflow.py` / `test_dockerignore.py`, so it rides the blocking `lint-and-test` CI job and the mandatory pre-push `uv run pytest -q` without a new workflow or pre-commit hook. Prose moves from the always-loaded `CLAUDE.md` into `docs/conventions/rationale.md`, `docs/subprojects/`, or the global `~/.claude/CLAUDE.md`, with a budget test forcing the discipline and an anchor test catching the links that breaks.

**Tech Stack:** Python 3.12, pytest (xdist, `-n 4`), ruff, `uv`. No new runtime dependencies; one new dev dependency (`pyyaml`) in Phase 3.

**Spec:** `docs/superpowers/specs/2026-09-22-rootstock-practices-adoption-design.md`

## Repository Layout

Two sibling checkouts. The plan names them by repo, never by absolute path:

- **bot** = `pr-review-bot` (this repo, where this plan lives)
- **wizard** = `onboarding-wizard` (the sibling checkout; CI resolves it via `PR_REVIEW_BOT_PATH`)

## Global Constraints

- **Budget:** `BUDGET_BYTES = 18_000`, `TOTAL_CAP_BYTES = 32_000`. Identical in both repos.
- **Exemption principle:** a section is budget-exempt only if it governs handling of credentials or secrets -- the operator's or a visitor's -- **and** trimming it to satisfy a byte budget would be a safety regression. Today that is `## Secret handling` in each repo, and nothing else.
- **Never trim the secret-handling section** to make a byte count pass. Never move unrelated prose into it to dodge the budget.
- **Before any push:** `uv run pytest -q` and `uv run ruff check .` green in the repo being pushed. Use `-q`, never `-v`.
- **Before any push to `main`:** invoke the `deploy-verify` skill. A green suite does not substitute for it.
- **Never modify `.claude/hooks/`** -- `check_env_access.py`, `redact_output.py`, `check_exfiltration.py` are untouched by this plan.
- **Never open `.env`** in either repo, for any reason, and never print a secret value.
- **ruff:** `line-length = 100`, `select = ["E4", "E7", "E9", "F", "E501"]`. Every code block below already fits.
- **pytest addopts:** bot `-n 4 ... --import-mode=importlib`; wizard `-n 4 --dist=loadgroup --import-mode=importlib`. Do not pass a bespoke `-n`.
- **Commit messages end with:**
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
  ```
- **Never commit unrelated pre-existing changes.** Check `git status` in the target repo before starting; if the tree is dirty with someone else's work, stop and report.

## Deviations from the spec, resolved during planning

The spec left three verification points open. All three are now resolved, two against what the spec assumed. **These supersede the spec where they conflict:**

1. **Tier C narrows to specs only.** The `writing-plans` skill states every plan **MUST start with** `# [Feature Name] Implementation Plan`. YAML front-matter above that heading violates it, and plan files are consumed by `executing-plans` / `subagent-driven-development`. Plans instead get a body-level index; only specs get front-matter. See Task 17.
2. **`update_bot_contract.py` resolves `origin/main`, not an arbitrary commit**, and rewrites contract and pin together or neither. The bot's Phase 2 must therefore be **pushed to `origin/main`**, not merely committed, before the wizard can bump. The script never stages and never commits, and its internal gate runs only `tests/test_bot_contract_parity.py` -- it will not run the new parity test. See Task 13.
3. **Heading slug rule, verified empirically** against all 10 anchors currently in use across both repos: lowercase, drop every character outside `[a-z0-9 -]`, then spaces to hyphens.

## Review Focus

Five failure modes the spec implies but that no task's primary deliverable exercises. Each has a test assigned to the task that owns the code.

1. **A `## ` heading written inside a fenced code block** would move a section boundary and silently change which bytes are exempt. Neither `CLAUDE.md` has a fence today, so this would appear only after a future edit. -- Task 2, `test_section_split_ignores_fenced_code_blocks`.
2. **CRLF line endings or a UTF-8 BOM** change the byte count without changing a single word, making the budget flap for reasons no reader can see. -- Task 2, `test_claude_md_has_no_crlf_line_endings`.
3. **Two headings that slugify identically** (GitHub appends `-1` to the second) mean an anchor link lands on the wrong section while a naive checker still sees a match. -- Task 1, `test_no_target_file_has_duplicate_heading_slugs`.
4. **An anchor link whose target file does not exist at all** is a different failure from a missing heading and must be reported as such, not swallowed by a `read_text` traceback. -- Task 1, `test_every_anchor_link_in_claude_md_resolves`.
5. **The parity test run against a pin that predates the shared sections** produces "section missing from the bot" -- indistinguishable from real drift unless the message says so. -- Task 13, `test_parity_failure_names_the_pinned_sha`.

---

# Phase 1 -- bot

No cross-repo dependency. Runs entirely in the bot checkout on branch `rootstock-practices-adoption` (already created; the spec is already committed there).

### Task 1: Anchor-integrity guard (bot)

`CLAUDE.md` deep-links into `rationale.md` by anchor and nothing validates those links -- `mkdocs.yml` sets `docs_dir: guide`, so `rationale.md`, `ISSUES.md` and everything under `docs/` are outside the `--strict` build entirely. This guard goes in **before** the relocations in Tasks 3-5, which break four of the six links.

**Files:**
- Create: `tests/test_doc_anchors.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_slug(heading_line: str) -> str`, reused verbatim (copied, not imported) by the wizard's copy in Task 6 and by `test_issues_index.py` in Task 14.

- [ ] **Step 1: Write the test file**

```python
"""CLAUDE.md deep-links into docs/conventions/rationale.md by anchor, and
nothing else checks those links. mkdocs --strict covers guide/ only
(mkdocs.yml sets docs_dir: guide), so rationale.md, ISSUES.md and everything
under docs/ sit outside that build entirely -- a renamed or relocated section
silently rots the link pointing at it, and CLAUDE.md is the one file every
session reads.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# `path/to/file.md#anchor`, the way CLAUDE.md spells them (inside backticks).
# The character classes exclude newlines, so a link wrapped across two source
# lines is not matched -- that is a false negative, never a false positive.
_ANCHOR_LINK_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)#([A-Za-z0-9-]+)")


def _slug(heading_line: str) -> str:
    """GitHub's heading slug.

    Verified empirically against every anchor currently in use in both this
    repo and the wizard: lowercase, drop every character outside
    [a-z0-9 -], then spaces to hyphens. Worked example --
    '## Docker image: no `chown -R` (2026-09-07)' becomes
    'docker-image-no-chown--r-2026-09-07' (the colon, backticks and
    parentheses vanish; the space inside 'chown -R' becomes the second
    hyphen of the doubled pair).
    """
    text = heading_line.lstrip("#").strip().lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return text.replace(" ", "-")


def _heading_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]


def _anchor_links() -> list[tuple[str, str]]:
    return _ANCHOR_LINK_RE.findall(CLAUDE_MD.read_text(encoding="utf-8"))


def test_claude_md_actually_contains_anchor_links():
    """A regex that silently stops matching would make every other test here
    pass vacuously. Pin that at least one link is found."""
    assert _anchor_links(), (
        "No `file.md#anchor` links found in CLAUDE.md. Either every deep link "
        "was removed (unlikely) or _ANCHOR_LINK_RE no longer matches how they "
        "are written -- fix the regex, do not delete this test."
    )


def test_every_anchor_link_in_claude_md_resolves():
    broken: list[str] = []
    for rel_path, anchor in _anchor_links():
        target = REPO_ROOT / rel_path
        if not target.exists():
            broken.append(f"{rel_path}#{anchor}: target file does not exist")
            continue
        slugs = {_slug(line) for line in _heading_lines(target)}
        if anchor not in slugs:
            broken.append(
                f"{rel_path}#{anchor}: no heading in {rel_path} slugifies to it"
            )
    assert not broken, (
        "CLAUDE.md has broken anchor links. A relocated or renamed section "
        "leaves the link behind pointing at nothing:\n  " + "\n  ".join(broken)
    )


def test_no_target_file_has_duplicate_heading_slugs():
    """Two headings slugifying identically means GitHub appends '-1' to the
    second, so a link lands on the wrong section while a naive checker still
    sees a match. Catch the ambiguity rather than the symptom."""
    offenders: list[str] = []
    for rel_path in sorted({path for path, _ in _anchor_links()}):
        target = REPO_ROOT / rel_path
        if not target.exists():
            continue
        seen: dict[str, str] = {}
        for line in _heading_lines(target):
            slug = _slug(line)
            if slug in seen:
                offenders.append(
                    f"{rel_path}: {seen[slug]!r} and {line!r} both slugify to {slug!r}"
                )
            else:
                seen[slug] = line
    assert not offenders, (
        "Duplicate heading slugs make anchor links ambiguous:\n  "
        + "\n  ".join(offenders)
    )
```

- [ ] **Step 2: Run the tests and confirm they pass against the current tree**

Run: `uv run pytest tests/test_doc_anchors.py -q`
Expected: PASS (4 tests). All six of this repo's anchors resolve today -- this guard is being installed *before* the refactor that breaks them, so green now is correct.

- [ ] **Step 3: Confirm the guard actually bites**

Temporarily rename one heading in `docs/conventions/rationale.md` (e.g. `## Docker image: no ` + backtick + `chown -R` + backtick + ` (2026-09-07)` to `## Docker image notes`), then run:

Run: `uv run pytest tests/test_doc_anchors.py -q`
Expected: FAIL, naming `docs/conventions/rationale.md#docker-image-no-chown--r-2026-09-07`.

Revert the rename with `git checkout docs/conventions/rationale.md` and re-run to confirm PASS again. **Do not commit the temporary rename.**

- [ ] **Step 4: Lint**

Run: `uv run ruff check tests/test_doc_anchors.py`
Expected: no findings.

- [ ] **Step 5: Commit**

```bash
git add tests/test_doc_anchors.py
git commit -m "$(cat <<'EOF'
Add an anchor-integrity guard for CLAUDE.md's deep links

CLAUDE.md links into docs/conventions/rationale.md by anchor and nothing
validated those links: mkdocs --strict covers guide/ only, so rationale.md
is outside that build entirely. A renamed section silently rots the link.

Installed before the relocations that break four of these six links, so the
breakage is caught by a test rather than by a reader clicking one later.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 2: CLAUDE.md budget test (bot)

**Files:**
- Create: `tests/test_claude_md_budget.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BUDGET_BYTES`, `TOTAL_CAP_BYTES`, `EXEMPT_HEADING_PREFIXES`, and `_sections(text) -> list[tuple[str, str]]`. The wizard's copy in Task 7 repeats this code with its own `EXEMPT_HEADING_PREFIXES` value.

- [ ] **Step 1: Write the test file**

```python
"""CLAUDE.md is the only project markdown loaded into every session before
any work begins, so every byte is paid whether or not the content is
relevant to the task at hand. This test pins that cost.

Sections governing credential/secret handling are exempt from the budget:
they are the one thing that must never be trimmed to make a byte count go
green. The separate whole-file cap is what stops "move it into the exempt
section" from becoming the way around the budget.

Overflow belongs in docs/conventions/rationale.md -- a one-line rule here,
the elaboration there -- never in a rule made vaguer to save bytes.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

BUDGET_BYTES = 18_000
TOTAL_CAP_BYTES = 32_000

# A section is budget-exempt only if it governs handling of credentials or
# secrets -- the operator's or a visitor's -- AND trimming it to satisfy a
# byte budget would be a safety regression. Nothing else qualifies. Adding
# an entry here is a deliberate act, not a way to make a red test go green.
EXEMPT_HEADING_PREFIXES = ("## Secret handling",)

_OVERFLOW_ADVICE = (
    "Move the elaboration into docs/conventions/rationale.md and leave a "
    "one-line rule plus a pointer behind. Do NOT trim the secret-handling "
    "section, and do NOT relocate unrelated prose into it to dodge this "
    "budget -- the whole-file cap exists to catch exactly that."
)


def _read() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def _sections(text: str) -> list[tuple[str, str]]:
    """Every '## ' section as (heading_line, full_section_text).

    A section runs from its own heading line to the next '## ' heading, so
    nested '### ' subsections belong to the '## ' section above them --
    which is what lets the wizard nest its visitor-credential rules inside
    ## Secret handling and have them inherit the exemption.

    Lines inside fenced code blocks are skipped: a '## ' written as an
    example inside a fence must not move a section boundary and silently
    change which bytes are exempt.
    """
    lines = text.splitlines(keepends=True)
    in_fence = False
    starts: list[int] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            starts.append(i)
    out: list[tuple[str, str]] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        out.append((lines[start].rstrip("\n"), "".join(lines[start:end])))
    return out


def _exempt_bytes(text: str) -> int:
    return sum(
        len(body.encode("utf-8"))
        for heading, body in _sections(text)
        if heading.startswith(EXEMPT_HEADING_PREFIXES)
    )


def test_exempt_headings_are_present():
    """Deleting or renaming an exempt heading would silently enlarge the
    budgeted region -- and the obvious way to "fix" a red budget is to make
    the exempt section bigger, not smaller. Fail loudly instead."""
    headings = [heading for heading, _ in _sections(_read())]
    for prefix in EXEMPT_HEADING_PREFIXES:
        assert any(heading.startswith(prefix) for heading in headings), (
            f"CLAUDE.md has no {prefix!r} section. The budget exempts that "
            "section by heading, so removing or renaming it changes what is "
            "measured. Restore the heading."
        )


def test_budgeted_bytes_are_within_budget():
    text = _read()
    budgeted = len(text.encode("utf-8")) - _exempt_bytes(text)
    assert budgeted <= BUDGET_BYTES, (
        f"CLAUDE.md's non-exempt content is {budgeted} bytes, over the "
        f"{BUDGET_BYTES}-byte budget by {budgeted - BUDGET_BYTES}. "
        + _OVERFLOW_ADVICE
    )


def test_whole_file_is_within_the_total_cap():
    size = len(_read().encode("utf-8"))
    assert size <= TOTAL_CAP_BYTES, (
        f"CLAUDE.md is {size} bytes, over the {TOTAL_CAP_BYTES}-byte "
        f"whole-file cap by {size - TOTAL_CAP_BYTES}. " + _OVERFLOW_ADVICE
    )


def test_claude_md_has_no_crlf_line_endings():
    """CRLF endings and a BOM change the byte count without changing a word,
    making the budget flap for a reason no reader can see in a diff."""
    raw = CLAUDE_MD.read_bytes()
    assert b"\r\n" not in raw, (
        "CLAUDE.md has CRLF line endings. Every line silently costs one "
        "extra byte against the budget. Convert to LF."
    )
    assert not raw.startswith(b"\xef\xbb\xbf"), (
        "CLAUDE.md starts with a UTF-8 BOM, which costs three bytes and "
        "breaks the '## Secret handling' prefix match on the first heading."
    )


def test_section_split_ignores_fenced_code_blocks():
    """A '## ' inside a fence is an example, not a section boundary. If it
    were treated as one, the exempt region would end early and unrelated
    prose would silently become exempt."""
    text = (
        "# Title\n"
        "## Secret handling\n"
        "real secret rules\n"
        "```markdown\n"
        "## Not a real heading\n"
        "```\n"
        "still inside secret handling\n"
        "## Conventions\n"
        "budgeted prose\n"
    )
    headings = [heading for heading, _ in _sections(text)]
    assert headings == ["## Secret handling", "## Conventions"]
    exempt = next(body for heading, body in _sections(text) if "Secret" in heading)
    assert "still inside secret handling" in exempt
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_claude_md_budget.py -q`
Expected: PASS (5 tests). This repo's budgeted remainder is 15,753 bytes today, already under 18,000 -- the test is a regression guard here, and the true driver in the wizard (Task 7).

- [ ] **Step 3: Confirm the budget actually bites**

Run: `uv run python -c "import tests.test_claude_md_budget as m; t=m._read(); print(len(t.encode()) - m._exempt_bytes(t), 'budgeted;', len(t.encode()), 'total')"`
Expected: roughly `15753 budgeted; 26782 total`. If the budgeted figure is not comfortably under 18,000, stop and report -- the relocations in Tasks 3-5 assume this starting point.

- [ ] **Step 4: Lint**

Run: `uv run ruff check tests/test_claude_md_budget.py`
Expected: no findings.

- [ ] **Step 5: Commit**

```bash
git add tests/test_claude_md_budget.py
git commit -m "$(cat <<'EOF'
Pin CLAUDE.md's size with a byte budget test

CLAUDE.md is the only project markdown loaded into every session, so its
size is a per-session cost paid whether or not the content is relevant.
Nothing noticed when it grew.

Sections governing credential/secret handling are exempt -- they are the one
thing that must never be trimmed to make a byte count pass -- and a separate
whole-file cap stops relocating prose into an exempt section from becoming
the way around the budget.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 3: Lesson entry format, and collapse the hygiene section (bot)

Item 4. Rewrites `rationale.md`'s hygiene entries into the ONE RIGHT WAY / TRIED / FAILED BECAUSE / DO INSTEAD shape, and collapses `CLAUDE.md`'s duplicate list to a pointer plus three one-liners. This renames the section's anchor, so Task 1's guard will go red until the link is updated in the same commit.

**Files:**
- Modify: `docs/conventions/rationale.md` -- the `## Plan-execution / multi-agent process hygiene: full detail` section
- Modify: `CLAUDE.md` -- the `## Plan-execution / multi-agent process hygiene` section

**Interfaces:**
- Consumes: Task 1's anchor guard.
- Produces: the anchor `plan-execution--multi-agent-process-hygiene-full-detail` is **kept unchanged** (do not rename the `rationale.md` heading), so no link fix is needed. Only the entry bodies change.

- [ ] **Step 1: Rewrite the rationale.md entries**

Keep the `## Plan-execution / multi-agent process hygiene: full detail` heading **exactly as it is** -- renaming it would rot the anchor for no gain. Replace the bulleted body with these `###` entries, in this order.

**Binding sourcing constraint:** `Tried` and `Failed because` must describe a real failure recorded in `ISSUES.md` or already narrated in the current rationale prose. Entry 7 has no incident behind it -- it is a measurement result -- so it keeps prose form and gets **no** fabricated TRIED line.

```markdown
### A brief's "stop and report" is a hard stop

**The one right way:** When a task brief says to stop and report, stop and
return control without fixing anything.

- **Tried:** An implementer hit an unpredicted failure the brief said to stop
  on, resolved it itself, and noted the deviation in its report afterward.
- **Failed because:** A controller reading a report after the fact cannot
  approve or reject work that has already been done. By the time it reads
  "I deviated because...", the deviation has happened.
- **Do instead:** Return control at the stop point, describing the failure
  with no fix applied.

### Re-read the whole passage after correcting part of it

**The one right way:** After changing one sentence of a multi-sentence rule,
re-read the entire passage for internal consistency.

- **Tried:** A targeted fix to a single clause in a multi-sentence passage.
- **Failed because:** The fix left a contradiction elsewhere in the same
  passage, invisible to the person who made it because they were looking at
  the clause they changed.
- **Do instead:** Re-read the whole passage, not just the edited clause.

### Task-scoped review checks conformance to the brief, not the brief

**The one right way:** Run the `code-review` skill against a task diff
immediately when it touches external-API or auth integration -- credential
construction, OAuth scopes, client setup -- as part of finishing that task.

- **Tried:** Relying on per-task reviews to catch bugs in code a plan handed
  the implementer verbatim.
- **Failed because:** A task-scoped review only asks "does this match what
  was asked", and matching the brief exactly does not mean the brief was
  right. The Vertex OAuth `scopes=` bug and the `list_vertex_models` SSRF
  both sailed through multiple per-task reviews before a final whole-branch
  review caught them.
- **Do instead:** Review that class of diff on the spot, at task time.

**Nuance:** Final whole-branch review remains a backstop, not a redundancy --
it is often the first review that would even think to distrust the plan's own
code.

### Write live-verification docs after the call, not before

**The one right way:** Documentation describing the outcome of a live
verification step is written after that step actually runs.

- **Tried:** Drafting the documentation of a live call's result in advance,
  from the plan's own task text.
- **Failed because:** The drafted text asserted a success that had not
  happened, and transcribing it published an unverified outcome as fact.
- **Do instead:** Treat a plan's description of a pending result as a
  placeholder to revise from the real outcome.

### Write the plan file inside the worktree

**The one right way:** When a plan is authored in the session that will
execute it via a worktree, write or commit that plan file inside the
worktree -- or commit it to the branch before creating the worktree.

- **Tried:** Writing a plan file into the main checkout, then running
  `git worktree add`.
- **Failed because:** A worktree materializes only committed content, so the
  new worktree could not see the file at all.
- **Do instead:** Commit first, or author inside the worktree.

### Check the target branch for uncommitted changes before merging

**The one right way:** Before merging a feature branch into any target
branch, run `git status` on the *target*, not only on the branch being
merged in.

- **Tried:** Merging after checking only the incoming branch.
- **Failed because:** A conflicting local edit or untracked file on the
  target failed the merge in a way that is confusing to diagnose from the
  merge error alone.
- **Do instead:** Inspect the target's working tree first.

### Don't reconfirm the full-suite baseline at the start of every task

**The one right way:** Trust the SDD ledger's last-recorded green state from
the prior task's own final run.

This entry has no incident behind it -- it is a measurement result, not a
failure, and is deliberately left in prose rather than dressed in the
TRIED/FAILED BECAUSE shape. The shared `subagent-driven-development`
implementer template already asks for exactly one full-suite run, right
before committing; a controller adding its own "first, confirm baseline"
instruction on top of that is a habit this project fell into, not something
the template requires. For a plan's first task, the worktree-setup step that
precedes dispatch normally already confirms green. Measured during the
2026-08-19/20 test-suite-performance work: the doubling was never
principled, and the case is weaker still now that the suite is faster (full
suite 57s serial to 35s at `-n 4`; the `-m "not db"` subset 31s to 20s --
see `docs/superpowers/specs/2026-08-19-test-suite-performance-design.md`
section 8).

**Nuance:** Add an explicit baseline-reconfirm instruction only when there is
a concrete reason to distrust the ledger for *this* task -- manual edits
since the last green run, a resumed session after a long gap, or a
worktree/branch switch -- never as a default precaution.

### Every parked finding goes in ISSUES.md before the branch is done

**The one right way:** Log every parked or deferred finding from a
task-scoped or whole-branch review in `ISSUES.md`'s Parked Issues section
before the branch is considered done.

- **Tried:** Leaving a deferred Minor finding in the SDD ledger, or in the
  session's own memory.
- **Failed because:** The ledger is deleted when the branch merges and the
  session ends with the conversation -- the finding is lost the moment the
  workspace is cleaned up.
- **Do instead:** Write it into `ISSUES.md` in that section's existing
  format.

**Nuance:** Log it even when a review explicitly judges a finding "no action
needed" or harmless-as-is. That judgment belongs in the entry's **Why
parked** line, not as a reason to skip logging.
```

- [ ] **Step 2: Collapse CLAUDE.md's hygiene section**

Replace the entire body of `## Plan-execution / multi-agent process hygiene` in `CLAUDE.md` (keep the heading) with exactly this. **Keep the wording of the three bullets character-for-character** -- Task 12 makes the wizard's copy byte-identical and Task 13 tests it.

```markdown
## Plan-execution / multi-agent process hygiene

Lessons from running Superpowers-style plans through subagent-driven
development. Each is stated in full, with the incident it generalizes from,
in `docs/conventions/rationale.md#plan-execution--multi-agent-process-hygiene-full-detail`
-- read that section before executing a plan. The three that cost the most
when missed:

- A task brief's "stop and report" instruction is a hard stop, not a suggestion — an implementer must actually stop, not self-resolve and mention the deviation afterward.
- Task-scoped review checks conformance to the brief, not correctness of the brief itself — run the `code-review` skill immediately on any task diff touching external-API/auth integration, don't defer to final review.
- Every parked/deferred Minor finding from a task-scoped or final whole-branch review must be logged in `ISSUES.md`'s Parked Issues section before the branch is considered done — including findings judged "no action needed."
```

- [ ] **Step 3: Run the guards**

Run: `uv run pytest tests/test_doc_anchors.py tests/test_claude_md_budget.py -q`
Expected: PASS. The anchor is unchanged, and `CLAUDE.md` shrank by roughly 1.2 KB.

- [ ] **Step 4: Confirm the size moved in the right direction**

Run: `uv run python -c "import tests.test_claude_md_budget as m; t=m._read(); print(len(t.encode()) - m._exempt_bytes(t))"`
Expected: roughly `14550` (down from ~15753).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/conventions/rationale.md
git commit -m "$(cat <<'EOF'
Rewrite the hygiene lessons in one-right-way form, collapse the CLAUDE.md copy

rationale.md recorded why each chosen way is right but rarely what was
burned reaching it. Each entry now leads with the one right way, then names
what was tried and the mechanism that made it fail.

The baseline-reconfirm entry keeps prose form deliberately: it comes from
measurement during the 2026-08-19/20 performance work, not from an incident,
and giving it a fabricated failure story would be a lie.

CLAUDE.md keeps a pointer plus the three highest-cost one-liners instead of
duplicating the whole list.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 4: Ledger-eligibility entry (bot)

Item 5. A documentation clarification that says no more than yes.

**Files:**
- Modify: `docs/conventions/rationale.md` -- new section
- Modify: `CLAUDE.md` -- extend the existing ledger bullet with a pointer

- [ ] **Step 1: Add the rationale.md section**

Append after the hygiene section:

```markdown
## Which repeatable checks are ledger-eligible (2026-09-22)

"Trust the ledger" is narrower than it sounds. It covers one check, and two
obvious-looking candidates are deliberately excluded.

**Eligible -- the full-suite `pytest`/`ruff` baseline within a plan's
execution.** Trust the SDD ledger's last-recorded green state rather than
reconfirming it at the start of every task. Unchanged; see the hygiene
section above.

**Not eligible -- `deploy-verify`.** It runs before every push to `main`,
unconditionally, because of the 2026-09-03 `python-multipart` deploy crash
that a green suite did not catch. It is also structurally unfit for a
ledger: the deploy image `COPY`s the whole tree, so "unchanged version" is
essentially never true at push time. A ledger here would either never hit or
hit wrongly, and weakening the rule would reopen a closed incident to buy
nothing.

**Not eligible -- the consumer-contract-lag job.** It is schedule-only and
advisory, so there is no manual re-run to prevent. A red run there is the
normal transient state between a contract change landing here and the
consumer catching up, and gating anything on it would invert the ownership
direction the cross-repo contract design establishes.
```

- [ ] **Step 2: Point CLAUDE.md's ledger bullet at it**

In `CLAUDE.md`'s hygiene section pointer paragraph, this is already covered by the section-level link added in Task 3. Add one sentence to the `## Conventions` section's existing `deploy-verify` bullet, immediately after "(see the skill for why, and the incident it generalizes from)":

```markdown
  `deploy-verify` is deliberately not ledger-eligible — see
  `docs/conventions/rationale.md#which-repeatable-checks-are-ledger-eligible-2026-09-22`.
```

- [ ] **Step 3: Run the guards**

Run: `uv run pytest tests/test_doc_anchors.py tests/test_claude_md_budget.py -q`
Expected: PASS. The new anchor `which-repeatable-checks-are-ledger-eligible-2026-09-22` must resolve -- if it does not, the heading text and the link disagree; fix the link, not the heading.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/conventions/rationale.md
git commit -m "$(cat <<'EOF'
State which repeatable checks are ledger-eligible, and which are not

"Trust the ledger" was ambiguous enough to look like it might cover
deploy-verify and the consumer-lag job. It covers neither: deploy-verify is
unconditional by incident and structurally unfit for a ledger since the
image COPYs the whole tree, and the lag job is schedule-only and advisory
with no manual re-run to prevent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 5: Relocate machine-local knowledge to global (bot)

Moves the Impeccable bridge out of the repo entirely, splits the workspace-isolation section, and de-hardcodes the checkout paths. **This is the task that breaks anchors** -- Task 1's guard is what catches it.

**Files:**
- Modify: `~/.claude/CLAUDE.md` (global, outside both repos, unversioned)
- Modify: `CLAUDE.md` -- remove `## Impeccable...`, split `## Workspace isolation...`, rephrase the hook-parity paths
- Modify: `docs/conventions/rationale.md` -- remove the Impeccable section, trim the workspace-isolation section

- [ ] **Step 1: Append the relocated content to the global CLAUDE.md**

Add to `~/.claude/CLAUDE.md`. The Impeccable text is moved **verbatim** from this repo's `rationale.md` section (script path, token path, router reasoning, fallback procedure, and the 2026-09-06 live-verification note) under a new `## Impeccable comp-first image generation (manual bridge)` heading, plus:

```markdown
## Worktree isolation vs the redaction wrapper

The redaction wrapper (`check_env_access.py` part 2) and the harness's
`EnterWorktree` isolation guard do not compose — every git command in an
`EnterWorktree` session gets refused once the wrapper is active, a bare
`git status` included. **Never use `EnterWorktree` while the wrapper lives.**
A worktree created manually with plain `git worktree add` and used from an
ordinary session runs git freely — the wrapper costs one *tool*, not the
workflow. Caveats: no `.env`/`.venv` in a worktree, `ExitWorktree` won't
clean up a manual one, and never `EnterWorktree --path` a manual worktree.

This applies to any repository carrying that hook, which is why it lives
here rather than in either project's own CLAUDE.md.
```

- [ ] **Step 2: Remove the Impeccable sections from the repo**

Delete `## Impeccable comp-first image generation (manual bridge)` from `CLAUDE.md` **and** the matching `## Impeccable comp-first image generation (manual bridge)` section from `docs/conventions/rationale.md`.

- [ ] **Step 3: Trim the workspace-isolation section in CLAUDE.md**

Replace the first paragraph of `## Workspace isolation: worktree vs inline` (the wrapper/`EnterWorktree` incompatibility and its `rationale.md` pointer) with:

```markdown
## Workspace isolation: worktree vs inline

`EnterWorktree` must never be used in these repos — the reason is harness-
level and lives in the global `~/.claude/CLAUDE.md`. Use a plain feature
branch, or a manual `git worktree add`.
```

Keep the `### Which to use` subsection exactly as it is -- it names this repo's own skills and test guards, and stays local.

- [ ] **Step 4: Fix the anchors this breaks**

Two links in `CLAUDE.md` now point at removed or altered `rationale.md` sections:
- `rationale.md#impeccable-comp-first-image-generation-manual-bridge` -- removed with the section it pointed at (Step 2 already deletes the sentence carrying it; confirm none remains).
- `rationale.md#workspace-isolation-measurement-detail-and-worktree-caveats` -- the `rationale.md` section stays (it holds the *measurement* detail, which is project-specific); keep the link from `### Which to use`.

- [ ] **Step 5: Rephrase the hardcoded checkout paths**

In `CLAUDE.md`'s hook-parity bullet, replace the two `~/`-prefixed paths:

```markdown
- **The `.claude/hooks/` files are shared with the sibling repo and must stay
  byte-identical.** This repo and the sibling onboarding-wizard checkout each
  carry their own copy of `check_env_access.py`, `redact_output.py` and
  `check_exfiltration.py`.
```

Leave the rest of that bullet (the drift incident, the `diff` requirement) unchanged.

- [ ] **Step 6: Run the guards**

Run: `uv run pytest tests/test_doc_anchors.py tests/test_claude_md_budget.py -q`
Expected: PASS. If `test_every_anchor_link_in_claude_md_resolves` fails naming the Impeccable anchor, a pointer to the deleted section survived somewhere -- remove it.

- [ ] **Step 7: Full suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS, no findings.

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md docs/conventions/rationale.md
git commit -m "$(cat <<'EOF'
Move machine-local knowledge out of the repo

The Impeccable comp-first image bridge is machine-local tooling by its own
description -- the script and its token live under ~/.config, nothing is in
pyproject.toml, and the router path is hand-rolled because huggingface_hub
isn't installed in that environment. It moves to the global CLAUDE.md, where
it also becomes available to the wizard's UI work, which never had it.

The EnterWorktree/redaction-wrapper incompatibility is harness knowledge that
was duplicated byte-for-byte in both repos; it moves to the global file once.
"Which to use" stays local -- it names this repo's own skills and guards.

The hook-parity bullet no longer hardcodes one machine's directory layout.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

---

# Phase 1 -- wizard

Runs in the **wizard** checkout on its own branch. No dependency on the bot's Phase 1 beyond the global `~/.claude/CLAUDE.md` edit from Task 5, which is shared and must not be repeated.

- [ ] **Phase gate:** In the wizard checkout, run `git status`. If the tree is dirty with pre-existing work, **stop and report** -- do not stash, do not commit. Then `git checkout -b rootstock-practices-adoption`.

### Task 6: Anchor-integrity guard (wizard)

Same guard as Task 1, plus one test the bot does not need: the wizard's `## Subsystem-specific rules (routing table)` links to `docs/subprojects/*.md` files by path, and Task 9 adds a new one.

**Files:**
- Create: `tests/test_doc_anchors.py` (in the wizard)

- [ ] **Step 1: Write the test file**

Copy the complete file from Task 1 Step 1 verbatim, then append this fifth test:

```python
_DOCS_FILE_RE = re.compile(r"`(docs/[A-Za-z0-9_./-]+\.md)`")


def test_every_docs_file_referenced_from_claude_md_exists():
    """CLAUDE.md's routing table sends a reader to a subsystem doc before a
    non-trivial change. A path that no longer resolves sends them nowhere,
    and the routing table is the only thing standing between an editor and
    rules that were deliberately moved out of the always-loaded file."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    missing = [
        rel_path
        for rel_path in sorted(set(_DOCS_FILE_RE.findall(text)))
        if not (REPO_ROOT / rel_path).exists()
    ]
    assert not missing, (
        "CLAUDE.md references docs files that do not exist:\n  "
        + "\n  ".join(missing)
    )
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_doc_anchors.py -q`
Expected: PASS (5 tests). All four of the wizard's anchors and all five routing-table targets resolve today.

- [ ] **Step 3: Lint and commit**

```bash
uv run ruff check tests/test_doc_anchors.py
git add tests/test_doc_anchors.py
git commit -m "$(cat <<'EOF'
Add an anchor-integrity guard for CLAUDE.md's deep links

Nothing validated CLAUDE.md's anchor links into rationale.md, or the routing
table's paths into docs/subprojects/. Installed before the relocations that
break them.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 7: CLAUDE.md budget test (wizard) -- starts RED

This is the task that drives the wizard's restructuring. Unlike the bot's copy, it fails on arrival: the wizard's budgeted remainder is 27,421 bytes against an 18,000-byte budget.

**Files:**
- Create: `tests/test_claude_md_budget.py` (in the wizard)

- [ ] **Step 1: Write the test file**

Copy the complete file from Task 2 Step 1 verbatim. `EXEMPT_HEADING_PREFIXES` stays `("## Secret handling",)` -- the wizard's visitor-credential rules become `###` subsections *inside* that section in Task 8, so they inherit the exemption without a second entry here.

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run: `uv run pytest tests/test_claude_md_budget.py -q`
Expected: **FAIL**, exactly one test (`test_budgeted_bytes_are_within_budget`), reporting roughly `27421 bytes, over the 18000-byte budget by 9421`. The other four pass.

If it fails for any other reason -- a missing heading, a CRLF finding -- stop and report. That is a different problem than the one this task exists to drive.

- [ ] **Step 3: Record the starting number**

Run: `uv run python -c "import tests.test_claude_md_budget as m; t=m._read(); print(len(t.encode()) - m._exempt_bytes(t))"`
Expected: roughly `27421`. Tasks 8 and 9 bring this under 18,000.

- [ ] **Step 4: Commit the failing test**

Committing a red test is deliberate here: it is the executable statement of what Tasks 8 and 9 must achieve, and the branch is not merged until it is green.

```bash
uv run ruff check tests/test_claude_md_budget.py
git add tests/test_claude_md_budget.py
git commit -m "$(cat <<'EOF'
Pin CLAUDE.md's size with a byte budget test (currently red)

This file is 27,421 budgeted bytes against an 18,000-byte budget. The test
lands red on purpose: it is the executable statement of the restructuring
the next two commits perform. The branch does not merge until it is green.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 8: Consolidate visitor-credential security under `## Secret handling` (wizard)

The wizard has two security domains. `## Secret handling` covers the operator's own secrets; the handling of *visitors'* credentials is scattered across `## Rules`, `## What the implementation adds to these rules`, and a bullet in the hygiene list. That scattering is why an earlier spec draft nearly relocated leak-prevention content behind a link to save bytes.

**Nothing in this task rewrites a rule.** Content moves; wording is preserved.

**Files:**
- Modify: `CLAUDE.md` (wizard)

- [ ] **Step 1: Move `## Rules` in as a subsection**

Cut the whole `## Rules` section. Paste it at the end of `## Secret handling` (immediately before the next `## ` heading), retitled:

```markdown
### Visitor-supplied credentials: the relay contract
```

Keep all four bullets verbatim. The section already describes itself as an extension of secret handling -- "same standard as this file's secret-handling section applies to the operator's own secrets, applied here to strangers' secrets" -- so no bridging prose is needed.

- [ ] **Step 2: Move the security-bearing half of `## What the implementation adds to these rules`**

From that section, move these three bullets verbatim into `## Secret handling` under a new heading:

```markdown
### What the implementation adds
```

The three to move:
1. The `RequestValidationError` handler living in `main.py`, not `router.py` (this is what turns a malformed request into a generic 422 instead of FastAPI's default, which echoes the rejected input including a submitted credential).
2. "A visitor's credential never touches `localStorage`", with its `STORAGE_KEYS` detail.
3. The `..._leaves_the_page_exactly_once` per-endpoint test convention, including the `callSupabaseRelay` adaptation.

Leave the now-empty `## What the implementation adds to these rules` heading in place for Task 9, which relocates whatever remains.

- [ ] **Step 3: Move the security half of the `replace=True` rule**

From `## The invariant this service now protects`, move the `replace=True` bullet's security reasoning into the new `### What the implementation adds` subsection as a fourth bullet:

```markdown
- **`update_frame(..., replace=True)` fully discards a frame's existing
  content instead of merging** — used only by the two endpoints that
  represent "start this frame over" (`validate-key`, `connect`). A plain
  merge on a resubmitted Render key or a Supabase reconnect would leave the
  *previous* account/project's `service_id`/`ref`/`database_url` sitting in
  the session, which `GET /api/session`'s completeness check (keyed off a
  field's mere presence) could then report as still-done for the wrong
  account. A new "start over" endpoint for a different frame uses
  `replace=True` too, not a plain merge.
```

**Leave the `asyncio.to_thread` wrapper rule where it is** -- it is a performance rule (calling `session_store.py` directly from an `async def` blocks the event loop), not a security one, and Task 9 keeps it as an inline stub.

- [ ] **Step 4: Move the SSRF rule out of the hygiene list**

Cut the SSRF bullet from `## Plan-execution / multi-agent process hygiene` and paste it into `## Secret handling` as:

```markdown
### SSRF: credential-accepting endpoints built from visitor-supplied structure

**A credential-accepting endpoint that constructs an auth/HTTP client object
from a visitor-supplied structured value (JSON, a config blob) needs an
explicit SSRF-focused check as part of its own design and review: does any
field in that structure influence which host a server-side request is made
to?** This is not covered by "returns a verdict, never the credential"
review — the vulnerable field is inert-looking routing metadata sitting next
to the credential in the same blob. That is exactly the shape of the
`list_vertex_models` SSRF (`token_uri`/`universe_domain` sitting beside the
service-account private key — see `ISSUES.md`). Ask this question explicitly
whenever a new credential-accepting frame is designed.
```

This is a security rule about a vulnerability class, not a plan-execution process lesson; it sat in the hygiene list only because that is where it happened to get written down after a review.

- [ ] **Step 5: Verify the move, and that nothing was reworded**

Run: `uv run pytest tests/test_claude_md_budget.py -q`
Expected: still FAIL on the budget, but the number has dropped to roughly `22180`. The exempt section grew by ~5.2 KB; nothing was deleted.

Run: `git diff --stat CLAUDE.md`
Expected: a large diff on one file. Then read the diff and confirm every removed line reappears elsewhere in the same file. **If any rule's wording changed, revert and redo** -- this task moves text, it does not edit it.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
Consolidate visitor-credential security under ## Secret handling

This service has two security domains: the operator's own secrets, and
strangers' credentials passing through the relay. Only the first had a named,
protected home; the second was scattered across ## Rules, ## What the
implementation adds, and a bullet in the hygiene list.

That scattering nearly cost something real -- the RequestValidationError
handler, the localStorage prohibition and the one-exit-path audit are leak
prevention, and a byte budget would have pushed them behind a link.

They become ### subsections of ## Secret handling, inheriting its budget
exemption without needing a second exempt heading. Content is moved
verbatim; no rule is reworded.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 9: Wizard relocations -- budget goes green

**Files:**
- Modify: `CLAUDE.md` (wizard)
- Modify: `docs/conventions/rationale.md` (wizard)
- Create: `docs/subprojects/session-store.md` (wizard)

- [ ] **Step 1: Split `## The cross-repo contract with the review-engine project`**

The bot already does this exact split. Move the elaboration into `docs/conventions/rationale.md` under a new section:

```markdown
## The cross-repo contract with the review-engine project (2026-09-10)
```

Keep roughly 1.5 KB inline in `CLAUDE.md` -- the ownership direction, the push-ordering rule, and the `update_bot_contract` requirement -- ending with a pointer to the new anchor `the-cross-repo-contract-with-the-review-engine-project-2026-09-10`.

- [ ] **Step 2: Move `## The invariant this service now protects` to a subsystem doc**

Create `docs/subprojects/session-store.md` holding the full section. In `CLAUDE.md`, leave this stub:

```markdown
## The invariant this service now protects

This backend holds a **server-side session** (`session_store.py`) in a
dedicated Postgres, identified by an `HttpOnly`/`Secure`/`SameSite=Lax`
cookie; every credential value is application-encrypted before it is
written. It replaced a stateless-relay design that mobile browsers broke by
destroying `sessionStorage` mid-flow. Full design, TTL behaviour and the
fork-risk hardening: `docs/subprojects/session-store.md`.

**Binding on any endpoint author, not just session work:** every `router.py`
endpoint calls `session_store.py` through the
`_get_session`/`_read_frame`/`_update_frame`/`_create_session`/`_delete_session`
wrappers at the top of `router.py`, never the `session_store.*` functions
directly. They exist to run the sync, real-Postgres-calling functions via
`asyncio.to_thread` — calling them directly from an `async def` endpoint
blocks the single event loop for every other concurrent request for the
duration of that DB round-trip.
```

Add the routing-table entry in `## Subsystem-specific rules (routing table)`:

```markdown
- Touching the **server-side session** (any endpoint that reads or writes session state)? Read `docs/subprojects/session-store.md`.
```

- [ ] **Step 3: Relocate what remains of `## What the implementation adds to these rules`**

After Task 8 took the three security bullets, move the remainder into `docs/conventions/rationale.md` and delete the now-empty `## ` heading from `CLAUDE.md`.

- [ ] **Step 4: Compress the Hebrew section to a pointer**

`rationale.md` already holds the full detail under `hebrew-strings-measurement-behind-the-nested-list-rule-2026-09-08`. Reduce `CLAUDE.md`'s section to two or three sentences plus that link.

- [ ] **Step 5: Apply Tasks 3, 4 and 5's changes to the wizard**

- Rewrite the wizard's `rationale.md` hygiene entries in the same ONE RIGHT WAY shape (Task 3 Step 1). The wizard's list no longer includes SSRF -- it moved in Task 8. **Its remaining entries are the same eight as the bot's; use the same text.**
- Collapse `CLAUDE.md`'s hygiene section to the pointer plus the same three one-liners, **character-for-character identical to the bot's** (Task 3 Step 2). Task 13 tests this.
- Add the ledger-eligibility section (Task 4 Step 1), **dropping the consumer-contract-lag paragraph entirely** -- the wizard has no such workflow -- and replacing it with a note that its own blocking CI job carries a pinned sibling checkout.
- Trim `## Workspace isolation: worktree vs inline`'s first paragraph exactly as Task 5 Step 3 does. Do **not** re-add anything to the global `~/.claude/CLAUDE.md`; Task 5 already did.
- Rephrase the hook-parity bullet's hardcoded paths (Task 5 Step 5).

- [ ] **Step 6: Run the guards -- both must now be green**

Run: `uv run pytest tests/test_claude_md_budget.py tests/test_doc_anchors.py -q`
Expected: **PASS**, all 10 tests.

Run: `uv run python -c "import tests.test_claude_md_budget as m; t=m._read(); print(len(t.encode()) - m._exempt_bytes(t))"`
Expected: roughly `12080`, comfortably under 18,000. Phase 2 adds ~2.2 KB on top.

If it is still over budget, the remaining candidates are listed in the spec's relocation table. **Do not** trim `### Visitor-supplied credentials`, any part of `## Secret handling`, or add a second exempt heading.

- [ ] **Step 7: Full suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS, no findings.

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md docs/conventions/rationale.md docs/subprojects/session-store.md
git commit -m "$(cat <<'EOF'
Relocate CLAUDE.md overflow, bringing the byte budget green

Moves the cross-repo contract elaboration and the session-store invariant
out of the always-loaded file, following the pattern the routing table
already documents: a subsystem's rules load when that subsystem is touched.
The wrapper requirement stays inline because it binds any endpoint author,
not only someone doing session work.

Also brings the hygiene rewrite, the ledger-eligibility entry and the
workspace-isolation trim into line with the bot. The ledger entry drops the
consumer-lag paragraph: this repo has no such workflow.

Budgeted bytes: 27,421 -> ~12,080 against an 18,000 budget.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

---

# Phase 2 -- shared rules and parity

**Depends on Phase 1 in both repos.** The hygiene lists only become identical once Task 8 has moved the SSRF rule out of the wizard's list, and the parity test cannot pass before that.

### Task 10: Contradiction Rule and cheap-probe rule (bot)

**Files:**
- Modify: `CLAUDE.md` (bot) -- two new sections after `## Conventions`

- [ ] **Step 1: Add both sections verbatim**

These two blocks are copied character-for-character into the wizard in Task 12 and tested byte-for-byte in Task 13. **Do not reword either repo's copy independently.**

```markdown
## When a request contradicts an established rule

If a request conflicts with a rule already written down — in this file, in
the global `~/.claude/CLAUDE.md`, in a committed spec under
`docs/superpowers/specs/`, in a memory file, or in a recorded decision in
`ISSUES.md` — **name the conflict and get explicit confirmation before
proceeding.** Never silently comply, and never silently pick a side.

State which rule it is, where it is written, what each reading would produce,
and which one you recommend. Then stop.

Two limits keep this from becoming an asking tax:

- **The rule must be written down.** A conflict with an unwritten preference
  or a stylistic nicety gets a judgment call and a one-line mention, not a
  block.
- **The conflict must be material** — proceeding under either reading
  produces work that is wrong under the other. This is one of the narrow
  cases where a blocking question is the correct move.

A reaffirmed request is the decision: proceed with the full request, and
record the resolution as a `feedback` memory so the same contradiction does
not have to be re-litigated in the next session.

## Probe cheaply before escalating

Before dispatching a subagent that will read many files or run a broad
review, state in one line which cheap probe you already ran — a grep, a
glob, a single targeted read — and why it was insufficient. If you have not
run one, run one first.

This is not a tax on genuine fan-out: "breadth unknown, a grep would need six
guesses" is a complete and acceptable answer. The rule exists to stop
reflexive escalation, not to litigate every dispatch.

The cost being managed is tokens and latency, both of which a broad agent
spends before returning anything — a grep that answers the question costs a
fraction of an agent that reads forty files to reach the same line.
```

- [ ] **Step 2: Run the budget guard**

Run: `uv run pytest tests/test_claude_md_budget.py tests/test_doc_anchors.py -q`
Expected: PASS. Budgeted bytes rise by roughly 2.2 KB to about 15,400 -- under 18,000 with roughly 2.6 KB to spare. **This repo is the binding constraint**; if it exceeds budget, relocate from `## Substitutions from the brief` or `## Module boundaries and contracts`, never from either new section.

- [ ] **Step 3: Full suite, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
Add the Contradiction Rule and the cheap-probe rule

Two behaviours that were being re-derived per session. The Contradiction
Rule fires only on a rule that is actually written down and only where the
conflict is material, so it does not become an asking tax; a reaffirmed
request closes it and gets recorded as a memory.

The cheap-probe rule is justified on token and latency cost, which is
repo-agnostic. The memory-pressure reasoning that originally motivated it is
specific to one machine and stays in the global CLAUDE.md.

Both sections are copied verbatim into the wizard and tested byte-for-byte
there.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 11: Land the bot on `origin/main`

The wizard's pin resolves `origin/main`, so this must be **pushed**, not merely merged locally.

- [ ] **Step 1: Confirm the branch is green**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS, no findings.

- [ ] **Step 2: Check the target branch before merging**

Run: `git checkout main && git status`
Expected: clean. If it is dirty with pre-existing work, **stop and report** -- do not stash or commit it.

- [ ] **Step 3: Merge**

```bash
git merge --no-ff rootstock-practices-adoption
```

- [ ] **Step 4: Run deploy-verify**

Invoke the `deploy-verify` skill. This is mandatory before any push to `main`, regardless of the change looking docs-only -- the rule is deliberately unconditional.

- [ ] **Step 5: Push**

```bash
git push origin main
git rev-parse origin/main
```

Record the printed sha. Task 13 pins against it.

### Task 12: Contradiction Rule and cheap-probe rule (wizard)

**Files:**
- Modify: `CLAUDE.md` (wizard)

- [ ] **Step 1: Copy both sections**

Copy the two blocks from Task 10 Step 1 **character-for-character**. Place them in the same position relative to `## Conventions`.

- [ ] **Step 2: Verify byte-identity locally before the test exists**

Run, from the wizard checkout:

```bash
python3 - <<'EOF'
from pathlib import Path
import re
def section(p, name):
    t = Path(p).read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(name)}$.*?(?=^## |\Z)", t, re.M | re.S)
    return m.group(0) if m else None
for name in ("When a request contradicts an established rule",
             "Probe cheaply before escalating",
             "Plan-execution / multi-agent process hygiene"):
    a = section("../pr-review-bot/CLAUDE.md", name)
    b = section("CLAUDE.md", name)
    print(f"{name}: {'MATCH' if a == b else 'DIFFERS'}")
EOF
```

Expected: `MATCH` on all three. If the hygiene section differs, Task 8's SSRF move or Task 9 Step 5's collapse is incomplete.

- [ ] **Step 3: Guards, suite, lint, commit**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS. Budgeted bytes rise to roughly 14,280.

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
Add the Contradiction Rule and the cheap-probe rule, matching the bot

Both sections are byte-identical to pr-review-bot's. Two subtly different
Contradiction Rules would be worse than one, because an agent could not tell
which reading governs.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 13: Shared-section parity test and pin bump (wizard)

**Files:**
- Create: `tests/test_claude_md_shared_sections_parity.py` (wizard)
- Modify: `.ci/pr-review-bot-ref`, `contracts/provisioning.json` (both rewritten by the script, together)

**Interfaces:**
- Consumes: the bot commit pushed in Task 11; `_require_bot_checkout()` / `_pinned_ref()` semantics mirrored from `tests/test_bot_contract_parity.py`.

- [ ] **Step 1: Write the test file**

```python
"""The Contradiction Rule, the cheap-probe rule and the hygiene one-liners are
repo-agnostic prose that must read identically in both repos. Two subtly
different Contradiction Rules would be worse than one, because an agent could
not tell which reading governs.

Convention alone is not enough here: CLAUDE.md records that the
.claude/hooks/ byte-parity convention already failed silently once, when
check_env_access.py drifted. This repo is the consumer in the cross-repo
ownership direction, so the check lives here and reads the bot through
.ci/pr-review-bot-ref -- the producer is never blocked on the consumer.

Helper resolution deliberately mirrors tests/test_bot_contract_parity.py
rather than importing from it, matching that file's own stated convention of
duplicating an ~8-line rule in each place that needs it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ".ci/pr-review-bot-ref"
CLAUDE_MD = "CLAUDE.md"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Every section here must be byte-identical across both repos.
SHARED_SECTIONS = (
    "When a request contradicts an established rule",
    "Probe cheaply before escalating",
    "Plan-execution / multi-agent process hygiene",
)


def _pinned_ref() -> str:
    text = (_REPO_ROOT / REF_PATH).read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1, f"{REF_PATH} must hold exactly one sha line, found {len(lines)}"
    assert _SHA_RE.match(lines[0]), f"{REF_PATH} is not 40 lowercase hex: {lines[0]!r}"
    return lines[0]


def _require_bot_checkout() -> Path:
    """The sibling pr-review-bot checkout -- FAILING, not skipping, in CI."""
    override = os.environ.get("PR_REVIEW_BOT_PATH")
    candidate = (
        Path(override) if override else Path(__file__).resolve().parents[2] / "pr-review-bot"
    )
    if candidate.is_dir() and (candidate / ".git").exists():
        return candidate
    message = f"no pr-review-bot git checkout at {candidate} (set PR_REVIEW_BOT_PATH)"
    if os.environ.get("CI"):
        pytest.fail(
            f"{message} -- required in CI, where ci.yml checks one out at the pinned "
            "ref. A skip here would report silent prose drift as a pass."
        )
    pytest.skip(f"{message} -- only runs where a checkout exists, always true in CI.")


def _extract_section(text: str, name: str) -> str | None:
    """The whole '## <name>' section, heading included, to the next '## '."""
    match = re.search(
        rf"^## {re.escape(name)}$.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return match.group(0) if match else None


def _describe_mismatch(name: str, mine: str | None, theirs: str | None, sha: str) -> str:
    """Why the two copies disagree, in the words that point at the real cause.

    A section missing from the BOT is far more often a pin that predates the
    section than real drift, and those two need opposite fixes -- say so
    rather than leaving a reader to guess from a diff of nothing.
    """
    if theirs is None:
        return (
            f"section '## {name}' is missing from pr-review-bot at the pinned "
            f"{sha}. The pin most likely predates that section: run "
            "`uv run python -m scripts.update_bot_contract` to advance it to "
            "origin/main. If the pin is already current, the section was "
            "removed over there and this repo's copy must follow."
        )
    if mine is None:
        return (
            f"section '## {name}' is missing from this repo's CLAUDE.md but "
            f"present in pr-review-bot at {sha}. Copy it over verbatim."
        )
    return (
        f"section '## {name}' differs from pr-review-bot at {sha}. These are "
        "repo-agnostic rules and must be byte-identical -- copy the bot's "
        "version verbatim rather than reconciling by hand."
    )


def _bot_claude_md(bot_path: Path, sha: str) -> str:
    """Read by git OBJECT, so a dirty or differently-checked-out sibling tree
    cannot affect the result."""
    result = subprocess.run(
        ["git", "-C", str(bot_path), "show", f"{sha}:{CLAUDE_MD}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, (
        f"cannot read {CLAUDE_MD} from pr-review-bot at {sha}: {result.stderr.strip()}"
    )
    return result.stdout


def test_shared_sections_are_byte_identical_to_the_bot():
    bot_path = _require_bot_checkout()
    sha = _pinned_ref()
    theirs_text = _bot_claude_md(bot_path, sha)
    mine_text = (_REPO_ROOT / CLAUDE_MD).read_text(encoding="utf-8")

    problems = []
    for name in SHARED_SECTIONS:
        mine = _extract_section(mine_text, name)
        theirs = _extract_section(theirs_text, name)
        if mine != theirs:
            problems.append(_describe_mismatch(name, mine, theirs, sha))
    assert not problems, "\n".join(problems)


def test_extract_section_stops_at_the_next_heading():
    text = "## One\nalpha\n\n## Two\nbeta\n"
    assert _extract_section(text, "One") == "## One\nalpha\n\n"
    assert _extract_section(text, "Missing") is None


def test_parity_failure_names_the_pinned_sha():
    """A stale pin and real drift need opposite fixes. The message must say
    which one it is, or a reader sees 'missing section' and edits prose that
    was never wrong."""
    sha = "b" * 40
    stale = _describe_mismatch("Probe cheaply before escalating", "mine", None, sha)
    assert sha in stale and "predates" in stale
    assert "update_bot_contract" in stale

    drift = _describe_mismatch("Probe cheaply before escalating", "mine", "theirs", sha)
    assert sha in drift and "byte-identical" in drift
```

- [ ] **Step 2: Run it and confirm it fails against the stale pin**

Run: `uv run pytest tests/test_claude_md_shared_sections_parity.py -q`
Expected: **FAIL** on `test_shared_sections_are_byte_identical_to_the_bot`, with a message naming the currently pinned sha and saying it likely predates the sections. The two unit tests pass.

This is the Review Focus #5 behaviour working as designed -- confirm the message reads that way before continuing.

- [ ] **Step 3: Bump the pin**

The script needs the bot's `origin/main` to already carry Task 11's push, and refuses to run if the contract or ref file is dirty.

```bash
uv run python -m scripts.update_bot_contract
```

Expected: prints `contracts/provisioning.json: <old> -> <new>` and the same for `.ci/pr-review-bot-ref`, ending with "Nothing was staged." The contract's *content* is unchanged by this work -- only the pin moves -- so the contract file may rewrite to identical bytes. That is expected, not a problem.

If it reports parity-test failures instead, it wrote neither file; read the output and stop.

- [ ] **Step 4: Confirm the pin now matches what was pushed**

Run: `cat .ci/pr-review-bot-ref`
Expected: the sha recorded in Task 11 Step 5.

- [ ] **Step 5: Re-run the parity test**

Run: `uv run pytest tests/test_claude_md_shared_sections_parity.py -q`
Expected: **PASS** (3 tests).

- [ ] **Step 6: Full suite, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add tests/test_claude_md_shared_sections_parity.py .ci/pr-review-bot-ref contracts/provisioning.json
git commit -m "$(cat <<'EOF'
Test the shared CLAUDE.md sections against the bot at the pinned ref

The Contradiction Rule, the cheap-probe rule and the hygiene one-liners are
repo-agnostic and must read identically in both repos. Convention alone
already failed once here -- check_env_access.py drifted silently -- so this
reuses the pinned sibling checkout CI already performs.

The check lives on this side only: the producer is never blocked on the
consumer. A stale pin and real drift are reported differently, because they
need opposite fixes.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

---

# Phase 3 -- front-matter

Retrieval cost only. Nothing here reduces upfront tokens -- `ISSUES.md` is never loaded upfront. If this phase is dropped, Phases 1 and 2 are unaffected.

### Task 14: ISSUES.md index test (bot) -- starts RED

**Files:**
- Create: `tests/test_issues_index.py` (bot)
- Modify: `pyproject.toml` (bot) -- add `pyyaml` to the dev group

- [ ] **Step 1: Declare pyyaml explicitly**

`yaml` currently resolves only transitively. In `[dependency-groups] dev`, add in alphabetical position:

```toml
    "pyyaml>=6.0",
```

Run: `uv sync --all-extras --dev`

- [ ] **Step 2: Write the test file**

```python
"""ISSUES.md is 100 KB+ and gets read repeatedly, which makes a whole-file
read one of the most expensive single tool results in a session. A front-
matter index lets a reader `head` the index and then `sed` one entry's range
instead.

An index that drifts is worse than none, because it is trusted. This test is
bidirectional on purpose: every indexed anchor must exist in the body AND
every body entry must appear in the index. Checking one direction alone lets
the index rot silently in the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUES_MD = REPO_ROOT / "ISSUES.md"

# Headings that are structure or templates, not incident entries.
_NON_ENTRY_HEADINGS = {"## <short title>", "### <short title>", "## Parked Issues"}


def _slug(heading_line: str) -> str:
    """GitHub's heading slug. Same rule as tests/test_doc_anchors.py."""
    text = heading_line.lstrip("#").strip().lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return text.replace(" ", "-")


def _split_front_matter(text: str) -> tuple[dict, str]:
    """The YAML front-matter block and the body after it."""
    assert text.startswith("---\n"), (
        "ISSUES.md must open with a '---' YAML front-matter block carrying "
        "read_index. Without it a reader has to pull the whole file to find "
        "one entry."
    )
    end = text.index("\n---\n", 3)
    return yaml.safe_load(text[4:end]), text[end + 5 :]


def _entry_headings(body: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if (line.startswith("## ") or line.startswith("### "))
        and line.strip() not in _NON_ENTRY_HEADINGS
    ]


def _index() -> list[dict]:
    front, _ = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    entries = front.get("read_index")
    assert isinstance(entries, list) and entries, (
        "front-matter has no non-empty 'read_index' list"
    )
    return entries


def test_every_index_entry_has_the_required_keys():
    for entry in _index():
        assert set(entry) >= {"anchor", "title", "parked"}, (
            f"index entry {entry!r} is missing one of anchor/title/parked"
        )
        assert isinstance(entry["parked"], bool), (
            f"index entry {entry['anchor']!r} has a non-boolean 'parked'"
        )


def test_every_indexed_anchor_exists_in_the_body():
    _, body = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    body_slugs = {_slug(h) for h in _entry_headings(body)}
    missing = [e["anchor"] for e in _index() if e["anchor"] not in body_slugs]
    assert not missing, (
        "read_index points at entries that no longer exist in ISSUES.md:\n  "
        + "\n  ".join(missing)
    )


def test_every_body_entry_appears_in_the_index():
    """The direction that actually rots: a new incident gets appended to the
    body and nobody touches the index, so the index quietly describes an old
    file while still looking authoritative."""
    _, body = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    indexed = {e["anchor"] for e in _index()}
    missing = [
        f"{_slug(h)}  ({h.strip()})"
        for h in _entry_headings(body)
        if _slug(h) not in indexed
    ]
    assert not missing, (
        "ISSUES.md entries are missing from read_index -- add one line per "
        "entry when you log it:\n  " + "\n  ".join(missing)
    )


def test_parked_flag_matches_where_the_entry_actually_sits():
    """'parked: true' is the whole point of the flag: it is how a reader
    skips forty deferred findings to reach the dozen real incidents."""
    _, body = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    parked_start = body.index("## Parked Issues")
    by_anchor = {entry["anchor"]: entry for entry in _index()}
    wrong = []
    for heading in _entry_headings(body):
        entry = by_anchor.get(_slug(heading))
        if entry is None:
            continue  # test_every_body_entry_appears_in_the_index owns this
        actually_parked = body.index(heading) > parked_start
        if entry["parked"] != actually_parked:
            wrong.append(
                f"{entry['anchor']}: index says parked={entry['parked']}, but "
                f"it sits {'after' if actually_parked else 'before'} "
                "'## Parked Issues'"
            )
    assert not wrong, "\n".join(wrong)
```

- [ ] **Step 3: Run and confirm the expected failure**

Run: `uv run pytest tests/test_issues_index.py -q`
Expected: **FAIL** -- every test errors on the missing front-matter block, with the message from `_split_front_matter`.

- [ ] **Step 4: Lint and commit the red test**

```bash
uv run ruff check tests/test_issues_index.py
git add tests/test_issues_index.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
Require a bidirectional read_index on ISSUES.md (currently red)

ISSUES.md is 100 KB+ and read repeatedly, so a whole-file read is one of the
most expensive single results in a session. An index makes a targeted read
possible.

Bidirectional deliberately: an index that drifts is worse than none, because
it is trusted. Declares pyyaml explicitly rather than relying on it
resolving transitively.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 15: Generate the bot's ISSUES.md index

**Files:**
- Modify: `ISSUES.md` (bot) -- prepend front-matter

- [ ] **Step 1: Generate the index from the current body**

Run this throwaway generator (it is not committed; the index is maintained by hand afterwards, with Task 14's test as the guard):

```bash
uv run python - <<'EOF'
import re, pathlib
p = pathlib.Path("ISSUES.md")
body = p.read_text(encoding="utf-8")
NON_ENTRY = {"## <short title>", "### <short title>", "## Parked Issues"}
def slug(h):
    t = h.lstrip("#").strip().lower()
    return re.sub(r"[^a-z0-9 \-]", "", t).replace(" ", "-")
parked_at = body.index("## Parked Issues")
lines = ["---", "read_index:"]
for h in body.splitlines():
    if not (h.startswith("## ") or h.startswith("### ")):
        continue
    if h.strip() in NON_ENTRY:
        continue
    title = h.lstrip("#").strip().replace('"', "'")
    parked = body.index(h) > parked_at
    lines.append(f"  - anchor: {slug(h)}")
    lines.append(f'    title: "{title[:110]}"')
    lines.append(f"    parked: {str(parked).lower()}")
lines.append("---")
p.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
print(f"indexed {sum(1 for x in lines if x.startswith('  - anchor:'))} entries")
EOF
```

Expected: exactly `indexed 52 entries`. (Verified against the current file: 57 headings minus the H1, the two `<short title>` template stubs, the `## Parked Issues` heading itself, and one further structural heading.) A different count means the body changed since this plan was written -- check the new entries look right before continuing.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_issues_index.py -q`
Expected: **PASS** (4 tests).

If `test_every_body_entry_appears_in_the_index` fails, two headings slugify identically -- disambiguate one heading's wording in `ISSUES.md` rather than dropping an index line.

- [ ] **Step 3: Sanity-check the index is actually usable**

Run: `head -20 ISSUES.md`
Expected: the front-matter block, readable on its own without the body.

- [ ] **Step 4: Full suite, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add ISSUES.md
git commit -m "$(cat <<'EOF'
Add a read_index to ISSUES.md

Lets a reader head the index and sed one entry's range instead of pulling
100 KB to find a single incident. Maintained by hand from here; the
bidirectional test is what stops it drifting.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JnkRHv3b3JYVLawYqJcy3F
EOF
)"
```

### Task 16: ISSUES.md index (wizard)

- [ ] **Step 1:** Add `"pyyaml>=6.0"` to the wizard's `[dependency-groups] dev`, then `uv sync --all-extras --dev`.
- [ ] **Step 2:** Create `tests/test_issues_index.py` in the wizard, copying the complete file from Task 14 Step 2 verbatim. The wizard's `ISSUES.md` has the identical two-tier structure -- `## <short title>` stub, `##` incidents, `## Parked Issues`, `###` parked entries -- so no changes are needed.
- [ ] **Step 3:** Run `uv run pytest tests/test_issues_index.py -q`. Expected: **FAIL** on the missing front-matter.
- [ ] **Step 4:** Run the generator from Task 15 Step 1 in the wizard checkout. Expected: exactly `indexed 52 entries`, the same as the bot.
- [ ] **Step 5:** Run `uv run pytest tests/test_issues_index.py -q`. Expected: **PASS**.
- [ ] **Step 6:** Run `uv run pytest -q && uv run ruff check .`. Expected: PASS, no findings.
- [ ] **Step 7:** Commit both the test and the indexed `ISSUES.md`, with the two commit messages from Tasks 14 and 15 combined into one.

### Task 17: Tier C -- front-matter on new specs only

**This narrows the spec.** The `writing-plans` skill requires every plan to *start with* `# [Feature Name] Implementation Plan`, so YAML front-matter above that heading violates the shared skill, and plan files are consumed by `executing-plans` / `subagent-driven-development`. Specs carry no such constraint.

**Files:**
- Modify: `docs/conventions/rationale.md` (both repos) -- record the convention and why plans are excluded

- [ ] **Step 1: Add the convention to both repos' rationale.md**

```markdown
## Front-matter on specs, and why not on plans (2026-09-23)

New spec documents under `docs/superpowers/specs/` open with a YAML
front-matter block carrying `title`, `date`, `status`
(`draft`/`accepted`/`superseded`) and a `sections` list of heading slugs, so
a reader can `head` the block and `sed` one section instead of reading a
50 KB document whole.

**Plans are deliberately excluded.** The shared `writing-plans` skill
requires every plan to *start with* `# <Feature> Implementation Plan`, and
plan files are consumed by `executing-plans` and
`subagent-driven-development`. Front-matter above that heading would violate
the skill's own contract. A plan's task headings already function as its
index; if a plan needs more, it goes in the body, not above the title.

**Existing documents are not retrofitted.** Roughly fifty historical plans
at 50-100 KB each are write-once artifacts; indexing them buys nothing. This
applies to documents created from 2026-09-23 onward.
```

- [ ] **Step 2: Apply it to this spec and plan**

Add the front-matter block to `docs/superpowers/specs/2026-09-22-rootstock-practices-adoption-design.md`. Leave this plan file unchanged -- it is the first document the exclusion applies to.

- [ ] **Step 3: Guards, suite, lint, commit in both repos**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS in both.

---

## Done when

- [ ] `uv run pytest -q` and `uv run ruff check .` green in **both** repos.
- [ ] `test_claude_md_budget.py` green in both, with the bot at roughly 15.4 KB and the wizard at roughly 14.3 KB against the 18,000-byte budget.
- [ ] `test_doc_anchors.py`, `test_claude_md_shared_sections_parity.py` and `test_issues_index.py` green.
- [ ] The wizard's `.ci/pr-review-bot-ref` points at the bot commit that carries the shared sections.
- [ ] Every parked or deferred review finding is logged in the relevant repo's `ISSUES.md` Parked Issues section -- including any judged "no action needed", whose reasoning goes in the **Why parked** line.
- [ ] `.claude/hooks/` unchanged in both repos: `diff <bot>/.claude/hooks/check_env_access.py <wizard>/.claude/hooks/check_env_access.py` prints nothing, and likewise for `redact_output.py` and `check_exfiltration.py`.
