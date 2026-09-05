# Design — `.env` protection hook hardening

**Date:** 2026-09-06
**Status:** Approved for planning
**Relates to:** `.claude/hooks/check_env_access.py`, `.claude/settings.json`
(`PreToolUse` hook registration), root `CLAUDE.md`'s "Secret handling"
section, `ISSUES.md`'s `grep -rn` walked-into-`.env` incident and the
Trust & Safety / secret-exposure incidents it documents, `config.py`
(`SettingsConfigDict(env_file=...)`), `scripts/_override.py` and
`providers/credentials.py` (both already use `dotenv_values()` directly —
the pattern this design reuses). Note: repo paths below are given relative
to the repo root as it exists post-restructure — there is no `bot/`
subdirectory any more; `config.py`, `scripts/`, `providers/`, etc. all live
at the repo root now.

## 1. Problem and context

The current `PreToolUse` hook (`check_env_access.py`) protects `.env` by
scanning tool-call arguments for the literal string `.env`: it denies
`Read`/`Edit`/`Write`/`NotebookEdit` calls whose path field matches, and
denies `Bash`/`PowerShell` calls whose full `command` string matches (after
carving out git/gh commit-message values, to keep documentation about
`.env` writable).

Two failure modes are structural, not tunable, for the shell-command half of
this check:

- **False positives.** Matching the literal substring `.env` anywhere in a
  shell command also catches it inside grep *patterns*, doc prose, and
  commit messages — hence the git/gh exemption logic already in the hook,
  and it still misfires today (a `--include`-scoped `grep` searching source
  for the string `.env` was blocked live during this design's own
  brainstorming session, even though it never touched the file).
- **False negatives.** A command that never types the string `.env` — `grep
  -rn foo .`, `find . -exec cat {} +`, a Python one-liner reading the
  file dynamically — is invisible to a hook that only inspects command
  *text*. This already happened for real: `ISSUES.md` documents a
  `grep -rn '\bscripts/' bot tests conftest.py` that recursively walked into
  `.env` and printed 3 (non-secret) lines from it, undetected by the
  hook, because the command's arguments never named `.env`.

A keyword filter over arbitrary shell text cannot be complete: the shell is
Turing-complete, so there is no bounded vocabulary of "ways to read a file."

**Two things ruled out during brainstorming, for the record:**

- **A dedicated low-privilege OS user running Claude Code**, isolated from
  `.env` by ACL, was considered as the strongest possible guarantee (kernel-
  enforced, tool-agnostic). Rejected as disproportionate infra churn for a
  single-user WSL2 dev box.
- **`chmod 000` on `.env` itself** was considered and rejected: `config.py`'s
  `SettingsConfigDict(env_file=(".env", ".env.config"))` means every local
  run of the app (a local `uvicorn` invocation, a test that instantiates a
  real `Settings()`) reads the file in-process, and `scripts/_override.py`
  / `providers/credentials.py` already call `dotenv_values()` on it
  directly as part of this project's own sanctioned credential-handling
  path. Since Unix permissions are per-*user*, not per-*program*, there is
  no way to let those reads through while denying `cat`/`grep -r` in the
  same shell as the same OS user — both are just `open()+read()` as the
  same principal.
  (The two other secret-bearing files identified in the same discussion —
  `github-app-private-key.pem` and `vertex-ai-private-key.json` — have no
  such in-process reader; only a one-shot, human-invoked
  `scripts.encode_credential` step ever opens them. Locking those down via
  file permissions is being handled separately, manually, by the user, and
  is **out of scope** for this design.)

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Structured tools (`Read`/`Edit`/`Write`/`NotebookEdit`) | **Unchanged.** Path-field deny stays exactly as it is today — no false-positive history, no reason to touch it. |
| Shell tools (`Bash`/`PowerShell`) — detection model | Replace the single "does the text mention `.env`" scan with **two independent checks**: a narrow mutation guard (deny), and a universal output-redaction wrapper (rewrite via `updatedInput`). |
| Mutation guard vocabulary | Deny only a closed set of verbs that would alter/relocate/delete `.env` itself: `rm`, `mv`, `cp` *onto* `.env`, output redirection (`>`/`>>`) *into* `.env`, `chmod`/`chown`/`setfacl` on `.env`, `sed -i`/`truncate` on `.env`. Everything else (grep patterns, doc prose, commit messages mentioning the word) is untouched by this check — it fires on verb+target shape, not on the substring appearing anywhere. |
| Output redaction mechanism | Every shell command not denied by the mutation guard is rewritten via `PreToolUse`'s `updatedInput.command` to pipe its real combined output through a redaction filter *before* the result reaches Claude, rather than scanning the command text for risk. |
| Redaction filter secret source | `dotenv_values(".env")` (python-dotenv) — reuses the exact parser this project's `scripts/_override.py` and `providers/credentials.py` already trust, and gives multi-line-value support for free (no live case today, since this project deliberately base64-encodes anything that would otherwise be multi-line, but nothing extra to build for it). |
| Redaction granularity | Match on secret **values**, not key names, above a minimum-length threshold of **8 characters** (to avoid pathological over-redaction of short/generic values like a single-digit slot index or `"true"`) — tunable later if real values in practice fall below it, but fixed as a concrete default now rather than left open. |
| Filter failure mode | **Fail closed.** Any internal error in the redaction filter (can't read `.env`, parse failure, unexpected exception) denies the whole command outright rather than letting output through unredacted. |
| Exit-code fidelity | `set -o pipefail` (bash) / `$LASTEXITCODE`-preserving equivalent (PowerShell) wrapped around a brace-grouped original command, so Claude still sees the *original* command's real success/failure — not the filter's — in the normal case. |
| `.env.config` | Never touched by anything in this design — it's non-secret by existing project convention and stays fully readable/editable as-is. |

## 3. Architecture

```
PreToolUse(tool=Bash|PowerShell)
        │
        ▼
  mutation guard: does `command` target .env with rm/mv/cp-onto/
  redirect-into/chmod/chown/setfacl/sed-i/truncate?
        │
    yes │                              no
        ▼                               ▼
     deny                    updatedInput.command =
   (existing                 "set -o pipefail; { <original>; } 2>&1 \
    _REASON                   | uv run --no-project python \
    message,                   .claude/hooks/redact_output.py"
    same shape                            │
    as today)                             ▼
                                 (command executes; its real
                                  stdout+stderr is piped through
                                  redact_output.py before Claude
                                  ever sees it)
```

`redact_output.py` runs as its own short-lived process for every wrapped
command:

1. Read stdin fully (the original command's real combined output).
2. Call `dotenv_values(env_path)` on `.env`, resolved relative to the
   project directory (same resolution convention the hook already uses via
   `${CLAUDE_PROJECT_DIR}`).
3. Build the set of values with length ≥ the minimum threshold.
4. For each such value, replace every literal occurrence in the buffered
   input with a fixed marker (e.g. `[REDACTED-SECRET]`).
5. Write the result to stdout, `exit 0`.
6. Any exception anywhere in steps 2–4 (missing file, parse error, unexpected
   type) → print a clear diagnostic to stderr and `exit 1` (deliberately
   *not* the buffered original text) — this is what makes the fail-closed
   behavior real: combined with `pipefail`, a filter crash surfaces as the
   whole wrapped command failing, and no unredacted text is ever emitted.

`Read`/`Edit`/`Write`/`NotebookEdit` path go through the existing,
unmodified deny logic — they never reach the redaction wrapper because they
don't execute a shell command at all.

## 4. Components

- **`.claude/hooks/check_env_access.py`** (modified) — keeps the existing
  `_PATH_FIELDS` deny logic verbatim. Replaces the current
  `_texts_to_check`/`_PATTERN` shell-command scanning with:
  - a small, closed-vocabulary mutation-guard matcher (deny), and
  - construction of the `updatedInput.command` rewrite (allow, with
    modification) for everything else.
  The git/gh message-exemption machinery (`_neutralize_git_message_values`
  and its heredoc/quote-shape helpers) is deleted — it existed solely to
  make the substring scan usable for commit messages, and the substring
  scan itself is going away.
- **`.claude/hooks/redact_output.py`** (new) — the filter process described
  above. Takes no arguments; reads stdin, writes stdout, resolves `.env`
  itself. Must be invoked the same portable way the existing hook already
  guarantees (`uv run --no-project python ...`), so it needs no dependency
  beyond what the hook's own invocation already provides — confirm
  `python-dotenv` is available in that context (it's already a dependency
  of this project's `pr-review-bot` package; `--no-project` must still
  resolve it, or the filter needs its
  own minimal parsing fallback — a task-level detail for the implementation
  plan, not a design ambiguity, since the fail-closed behavior means "can't
  import dotenv" already degrades safely to "deny the command" rather than
  to a silent leak).
- **`.claude/settings.json`** — no change; the same `PreToolUse` hook
  registration already routes every `Bash`/`PowerShell` call through
  `check_env_access.py`.

## 5. Error handling

- Mutation guard false negative risk (a mutating command shaped in a way
  the closed vocabulary doesn't recognize) is accepted as a known residual
  risk, same posture the current hook already has for anything outside its
  own vocabulary — the guard is deliberately narrow specifically to avoid
  reintroducing the false-positive problem this design exists to fix.
- Redaction filter crash → fail closed (Section 3, step 6) — the command is
  denied, never silently unredacted.
- `.env` missing entirely (e.g. a fresh checkout before setup) → treated
  the same as any other filter error: fail closed. This will surface loudly
  the first time a Bash command runs before setup is complete; acceptable,
  matches the project's existing "loud, not silent" posture, and is a
  one-time setup-order issue rather than a routine failure mode.

## 6. Testing

- Unit tests for `check_env_access.py`'s mutation guard: each verb in the
  closed vocabulary, targeting `.env`, is denied; the same verbs targeting
  `.env.config`/`.env.example`/an unrelated file are not; a command that
  merely mentions the word `.env` in prose (grep pattern, `git commit -m`)
  is not denied and is *not* rewritten by the mutation-guard branch (falls
  through to the redaction-wrapper branch, which is a no-op for output that
  contains no real secret value).
- Unit tests for `redact_output.py`: a real secret value present in stdin is
  replaced; a value below the minimum-length threshold is left alone; a
  multi-line value (constructed via a quoted `dotenv_values()`-parsed test
  fixture, not a real project secret) is fully replaced, newlines included;
  a corrupted/missing `.env` fixture causes a non-zero exit and no output
  passthrough.
- Integration-style test (or manual verification during implementation) of
  the full wrapped pipeline: run the hook's `updatedInput` rewrite through
  an actual bash invocation and confirm (a) exit code fidelity for both a
  succeeding and a failing original command, and (b) a `grep -rn` style scan
  that walks into a synthetic `.env` fixture comes back redacted rather than
  denied outright — reproducing the exact `ISSUES.md` incident shape as a
  regression test.
