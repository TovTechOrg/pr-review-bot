"""PreToolUse hook: protects `.env`.

Two independent mechanisms:
1. Read/Edit/Write/NotebookEdit/Grep/Glob calls whose file_path/path/
   notebook_path field names `.env` are denied outright -- these tools
   don't go through a shell, so there's no way to "wrap" them; blocking is
   the only lever, and there's no legitimate reason for Claude to touch
   `.env` through one of these tools.
2. Every Bash/PowerShell command is rewritten (via `updatedInput`) to run
   its real combined output through `redact_output.py` before the result
   reaches Claude -- see that script's own docstring, and
   docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md for why
   this replaced the previous text-scanning approach entirely (neither
   complete nor precise for an open-ended shell command). Bash feeds the
   filter from a private file rather than a pipe, so that the rewritten
   command stays runnable inside a worktree-isolated session; see
   `_wrap_bash` and
   docs/superpowers/specs/2026-09-12-redaction-wrapper-worktree-design.md.
   PowerShell still pipes -- it is unused on this machine and the shape was
   left alone rather than changed untested.

Mutation prevention (rm/mv/chmod/... targeting `.env`) is NOT this hook's
job -- it's enforced at the OS level via `chmod 400` + `chattr +i` on
`.env` (applied manually, outside this hook; see the design doc). A
mutating command that still reaches the shell just fails with a normal OS
permission error, which is harmless output and passes through the
redaction wrapper like anything else.

Matches `.env` as a path component -- preceded by start-of-string or a
non-identifier/non-dot character, followed by end-of-string or the same --
so `.env.example`, `.env.config`, and `.env.config.example` are excluded.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

_PATTERN = re.compile(r"(?:^|[^A-Za-z0-9_.])\.env(?:[^A-Za-z0-9_.]|$)")

# Fields that name a path the tool will act on directly -- checked for
# every tool, since several (Read, Edit, Write, NotebookEdit, Grep, Glob)
# use one of these names for "the file/dir in play".
_PATH_FIELDS = ("file_path", "path", "notebook_path")

_REASON = (
    "Blocked: this tool call operates on .env. CLAUDE.md's absolute rule -- "
    "never touch .env with any tool, full stop, not even a narrow/safe-looking "
    "pattern (see the Secret handling section). Ask the user to check or edit "
    "it themselves."
)

# Claude Code's only two shell-executing tools.
_SHELL_TOOLS = ("Bash", "PowerShell")

_HOOK_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _HOOK_DIR.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_REDACT_SCRIPT = _HOOK_DIR / "redact_output.py"

# Where a wrapped command's unredacted combined output lands for the moment
# between the command finishing and the filter reading it. Outside the repo so
# it can never be committed; 0700 so nothing else on the box can read it.
_SINK_DIR = Path.home() / ".cache" / "pr-review-bot-redact"
_SINK_MAX_AGE_S = 3600


def _deny() -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _REASON,
        }
    })


def _allow_with_rewrite(command: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": command},
        }
    })


def _ps_quote(value: str) -> str:
    """PowerShell single-quoted string literal: no interpolation of any
    kind, embedded single quotes doubled -- PowerShell's own escaping rule."""
    return "'" + value.replace("'", "''") + "'"


def _sweep_sinks() -> None:
    """Backstop for a wrapped command killed before it reached its own `rm`,
    which would otherwise leave unredacted output sitting on disk
    indefinitely. An EXIT trap would be the tidier mechanism, but the
    worktree-isolation guard rejects `trap` outright -- it is handed shell
    text the guard cannot prove is git-free -- so cleanup is an ordinary
    command on the normal path plus this sweep for the abnormal one.
    Best-effort: a sweep failure must never stop a command from being
    wrapped."""
    cutoff = time.time() - _SINK_MAX_AGE_S
    try:
        for stale in _SINK_DIR.iterdir():
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
    except OSError:
        pass


def _sink_path() -> Path:
    """Creates the output sink HERE rather than in the wrapped shell. Two
    reasons, both load-bearing: a shell-side `$(mktemp)` is a value computed
    at runtime, which the harness's worktree-isolation guard refuses as a
    redirect target, whereas a path baked in here is a literal it can read;
    and creating it via mkstemp (O_EXCL, 0600) leaves no window for a
    symlink race on a shared temp directory."""
    _SINK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sweep_sinks()
    fd, name = tempfile.mkstemp(prefix="out-", dir=_SINK_DIR)
    os.close(fd)
    return Path(name)


def _wrap_bash(command: str) -> str:
    """Runs `command` completely unmodified inside a subshell (preserves its
    own internal &&/||/pipes/heredocs exactly as written), sends its combined
    output to a private 0600 file, and runs redact_output.py with that file
    on stdin.

    A subshell, not a brace group, so that a bare `exit` in `command` ends
    only `command` and the filter still runs -- a brace group would exit the
    wrapper itself and silently drop the output. This also matches the
    previous shape's semantics exactly, since bash already ran the left side
    of its pipeline in a subshell: `cd` and variable side effects did not
    leak out then either, and `command`'s own EXIT trap stays scoped to
    `command` rather than clobbering the cleanup trap below.

    A file rather than a pipe for an external reason: the harness's
    worktree-isolation guard rejects a pipe whose reading end is a program
    it has no model for, so the previous `... | uv run ... python
    redact_output.py ...` shape was refused for EVERY command inside a
    worktree session, `true` included (see ISSUES.md, 2026-09-11, and
    docs/superpowers/specs/2026-09-12-redaction-wrapper-worktree-design.md
    for the alternatives weighed). That guard accepts `< /literal/path`,
    which is what lets both mechanisms hold at once.

    With no pipe there is no `set -o pipefail` to reason about: the
    original command's status is captured directly, and a filter failure
    still overrides it, so a redaction error can never surface as a success
    carrying unfiltered content. `--directory` pins the filter's `uv run`
    invocation to the project root regardless of the wrapped command's own
    cwd side effects."""
    sink = shlex.quote(str(_sink_path()))
    return (
        "( " + command + "\n) > " + sink + " 2>&1\n"
        "__redact_rc=$?\n"
        f"uv run --no-project --directory {shlex.quote(str(_PROJECT_ROOT))} "
        f"python {shlex.quote(str(_REDACT_SCRIPT))} {shlex.quote(str(_ENV_PATH))} "
        f"< {sink}\n"
        "__redact_filter_rc=$?\n"
        f"rm -f {sink}\n"
        "if [ $__redact_filter_rc -ne 0 ]; then exit $__redact_filter_rc; fi\n"
        "exit $__redact_rc"
    )


def _wrap_powershell(command: str) -> str:
    """PowerShell has no direct pipefail equivalent for a native-executable
    pipeline (`$LASTEXITCODE` after a pipe reflects only the last native
    process run, which would be the filter, not the original command).
    Instead: capture the original command's output AND `$LASTEXITCODE` via
    a pure-cmdlet pipe stage (`Out-String`, which never touches
    `$LASTEXITCODE`), run the filter as a separate stage afterward, and
    exit with the filter's code if it failed, else the original's."""
    return (
        "$__redact_out = & { " + command + " } 2>&1 | Out-String\n"
        "$__redact_code = $LASTEXITCODE\n"
        f"$__redact_out | uv run --no-project --directory {_ps_quote(str(_PROJECT_ROOT))} "
        f"python {_ps_quote(str(_REDACT_SCRIPT))} {_ps_quote(str(_ENV_PATH))}\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } else { exit $__redact_code }"
    )


def main() -> int:
    payload = sys.stdin.read()
    try:
        data = json.loads(payload)
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input") or {}
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Malformed input is unexpected -- fail toward checking the whole
        # raw payload rather than silently skipping the check entirely.
        if _PATTERN.search(payload):
            print(_deny())
        return 0

    path_texts = [str(tool_input[field]) for field in _PATH_FIELDS if field in tool_input]
    if any(_PATTERN.search(t) for t in path_texts):
        print(_deny())
        return 0

    if tool_name in _SHELL_TOOLS and "command" in tool_input:
        command = str(tool_input["command"])
        wrap = _wrap_powershell if tool_name == "PowerShell" else _wrap_bash
        print(_allow_with_rewrite(wrap(command)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
