# Redaction wrapper vs. worktree isolation (2026-09-12)

Supersedes the Bash half of the wrapper shape in
`2026-09-06-env-hook-hardening-design.md`. Nothing about *what* is redacted
changes -- only how the filter is fed.

## The collision

`check_env_access.py` rewrites every `Bash` command so its combined output
passes through `redact_output.py` before reaching Claude. The harness's
worktree-isolation guard independently re-parses the command a session is
about to run and refuses anything it cannot prove keeps `git` inside the
worktree. The guard runs *after* the hook, on the rewritten text.

The old shape ended in `... | uv run --no-project --directory <root> python
redact_output.py <root>/.env`. The guard refuses a pipe whose reading end is
a program it has no model for, on the grounds that such a program could run
git from its stdin. `uv` is such a program. So **every** Bash call inside a
worktree session was refused, `true` included -- see `ISSUES.md`, 2026-09-11.

## What changed

The filter is fed from a file instead of a pipe:

```
( <command>
) > <sink> 2>&1
__redact_rc=$?
uv run --no-project --directory <root> python redact_output.py <env> < <sink>
__redact_filter_rc=$?
rm -f <sink>
if [ $__redact_filter_rc -ne 0 ]; then exit $__redact_filter_rc; fi
exit $__redact_rc
```

Four details are load-bearing, each for a reason that is not obvious from
reading the line it appears on:

- **A file, not a pipe.** The guard's redirect check accepts `< /literal/path`.
  This is the single change that makes the wrapper runnable in a worktree.
- **The sink is created by the hook**, via `mkstemp` in a 0700 directory
  outside the repo. A shell-side `$(mktemp)` is a value computed at runtime,
  which the guard refuses as a redirect target; a path baked in by the hook is
  a literal it can read. Creating it with `O_EXCL`/0600 also closes the
  symlink race a shared temp directory would otherwise open.
- **A subshell, not a brace group.** A bare `exit` in `<command>` must end
  only `<command>`; under `{ }` it would exit the wrapper itself, skip the
  filter, and silently drop the output. This also preserves the old shape's
  semantics exactly, since bash already ran the left side of a pipeline in a
  subshell -- `cd` and variable side effects did not leak out then either --
  and it keeps a caller's own `EXIT` trap from clobbering cleanup.
- **`rm` on the normal path, plus a sweep for the abnormal one.** An `EXIT`
  trap would be tidier, but the guard rejects `trap` outright as shell text it
  cannot prove is git-free. `_sweep_sinks()` removes anything older than an
  hour, covering a command killed before it reached its `rm`.

`set -o pipefail` is gone with the pipe; the original command's status is now
captured directly, and a filter failure still overrides it.

## What this does NOT fix: git in a worktree

Worktree sessions can now run everything **except** commands naming git. That
is not a flaw in the shape above, and no reshaping fixes it. The guard has two
modes, selected by the command's shape:

- A **plain single command** named git takes a fast path
  (`if (n.simple && RP.test(me)) return null`) and is handed to a precise
  checker that validates `-C`, `--git-dir` and cwd.
- **Anything else** -- a pipe, a list, a redirect, a second statement --
  falls through to a conservative path that refuses any token matching
  `/^git(\.exe|\.real|-[a-z][\w-]*)?$/i`, unconditionally.

Redaction requires at minimum a redirect and a second command, so a wrapped
command can never be "plain". The guard's own advice, "split it into plain,
separate commands", is advice a rewriting hook structurally cannot take.

## Alternatives rejected

- **Hand the original command to `redact_output.py` as an argument for it to
  spawn.** This satisfies the guard completely -- because the guard can no
  longer see the real command at all. Passing a check by blinding it is not
  passing it, and it would disable the guard everywhere, not just here.
- **Exempt a single plain git command from wrapping**, so it stays "plain" and
  worktrees become fully usable. Rejected for now because it reintroduces
  exactly the judgment call the mechanical hook exists to remove: git output
  *should* not carry `.env` values, since `.env` is gitignored and never
  committed, but "should" is the assumption this whole mechanism was built to
  stop relying on. Recorded as an option, not taken unilaterally.
- **Keep the pipe and stop using worktrees.** The status quo this replaces.
  Still the fallback if the file-based shape ever causes trouble.

## Cost accepted

The command's unredacted combined output lands in a 0600 file, in a 0700
directory outside the repo, for the moment between the command finishing and
the filter reading it. Previously it only ever lived in a pipe buffer. The
exposure is local-disk, same-user, and bounded by `rm` plus the sweep. Note
that a command the guard *refuses* still leaves its (empty, never-written)
sink behind until the sweep runs, since the command never executes.

## Pinned by

- `tests/test_check_env_access_hook.py` -- subshell, no pipe, no `trap`,
  literal sink path, filter-failure override.
- `tests/test_env_hook_redaction_integration.py` -- executes the real wrapped
  command: bare `exit` still reaches the filter, a filter failure suppresses
  output and overrides a successful command, the sink is 0600 and is removed
  afterwards.
