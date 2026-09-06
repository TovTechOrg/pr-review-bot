"""PreToolUse hook: protects `.env`.

Two independent mechanisms:
1. Read/Edit/Write/NotebookEdit/Grep/Glob calls whose file_path/path/
   notebook_path field names `.env` are denied outright -- these tools
   don't go through a shell, so there's no way to "wrap" them; blocking is
   the only lever, and there's no legitimate reason for Claude to touch
   `.env` through one of these tools.
2. Every Bash/PowerShell command is rewritten (via `updatedInput`) to pipe
   its real combined output through `redact_output.py` before the result
   reaches Claude -- see that script's own docstring, and
   docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md for why
   this replaced the previous text-scanning approach entirely (neither
   complete nor precise for an open-ended shell command).

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
import re
import shlex
import sys
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


def _wrap_bash(command: str) -> str:
    """Runs `command` completely unmodified inside a brace group (preserves
    its own internal &&/||/pipes/heredocs exactly as written), merges
    stderr into stdout, and pipes the combined output through
    redact_output.py. `set -o pipefail` makes the overall exit status
    reflect the ORIGINAL command's success/failure, not the filter's, in
    the normal case (confirmed: `set -o pipefail; { false; } 2>&1 | cat`
    exits 1; `{ true; } 2>&1 | cat` exits 0). `--directory` pins the
    filter's `uv run` invocation to the project root regardless of the
    wrapped command's own cwd side effects."""
    return (
        "set -o pipefail; { " + command + "\n} 2>&1 | "
        f"uv run --no-project --directory {shlex.quote(str(_PROJECT_ROOT))} "
        f"python {shlex.quote(str(_REDACT_SCRIPT))} {shlex.quote(str(_ENV_PATH))}"
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
