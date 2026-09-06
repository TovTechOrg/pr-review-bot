# .env Protection Hook Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `.claude/hooks/check_env_access.py`'s incomplete "does the
shell command mention `.env`" text scan with a universal output-redaction
wrapper, so that `.env` secret exposure is prevented by content (complete by
construction) instead of by pattern-matching an open-ended shell command
(neither complete nor precise).

**Architecture:** `Read`/`Edit`/`Write`/`NotebookEdit` calls targeting `.env`
keep being denied outright via their path field (unchanged). Every
`Bash`/`PowerShell` command is unconditionally rewritten, via `PreToolUse`'s
`updatedInput`, to pipe its real combined output through a new script
(`redact_output.py`) that replaces any real `.env` secret value with a fixed
marker before the result ever reaches Claude. Mutation prevention
(`rm`/`chmod`/etc. targeting `.env`) is handled entirely outside this hook,
by the user applying `chmod 400` + `chattr +i` to `.env` directly — not part
of this plan's scope.

**Tech Stack:** Python 3.12 (stdlib `json`/`re`/`shlex`/`subprocess`/`pathlib`
for the hook scripts and their tests), `python-dotenv`'s `dotenv_values()`
(already a project dependency), `pytest`, bash (`set -o pipefail` + brace
groups), PowerShell (`$LASTEXITCODE` capture) — no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md`

## Global Constraints

- Redaction marker text: `[REDACTED-SECRET]` (exact string, used by every task).
- Minimum secret length for redaction: **8 characters** — a value shorter
  than this is left alone.
- The redaction filter **fails closed**: any internal error (missing `.env`,
  wrong argument count, unexpected exception) prints a diagnostic to stderr
  and exits 1, never printing any of its buffered stdin.
- The wrapped Bash invocation always uses
  `uv run --no-project --directory <project_root> python <redact_script_abs> <env_path_abs>`
  — the explicit `--directory` is required so the invocation doesn't depend
  on whatever cwd the Bash tool happens to be in when the wrapped command
  actually runs (confirmed live: `uv run --no-project` alone fails to find
  `python-dotenv` when invoked from a directory with no nearby `.venv`, e.g.
  after a `cd`; `--directory <project_root>` fixes this).
- `.env.config`/`.env.example`/`.env.config.example` are never affected by
  anything in this plan — the existing path-boundary regex
  (`(?:^|[^A-Za-z0-9_.])\.env(?:[^A-Za-z0-9_.]|$)`) already excludes them and
  is not changing.
- No task in this plan ever reads, prints, or constructs a literal value
  from the project's real `.env` — every test uses a synthetic `.env`
  fixture under `tmp_path`.
- This plan does **not** include applying `chmod 400`/`chattr +i` to the
  real `.env` — that's a manual, out-of-band step for the user, per the
  spec's own scoping. See the handoff note at the end of this plan.

---

### Task 1: `redact_output.py` — the redaction filter

**Files:**
- Create: `.claude/hooks/redact_output.py`
- Test: `tests/test_redact_output_hook.py`

**Interfaces:**
- Consumes: nothing from other tasks (this is the first task).
- Produces: `redact_output.py` is invoked as
  `python .claude/hooks/redact_output.py <env_path>` (exactly one
  positional argument, the absolute path to a `.env`-shaped file), reading
  the text to redact from stdin and writing the redacted text to stdout.
  Exit 0 on success, exit 1 on any internal failure (nothing printed to
  stdout in that case). Task 2 constructs command lines that invoke this
  exact interface.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_redact_output_hook.py`:

```python
"""Exercises .claude/hooks/redact_output.py via subprocess -- mirrors how
check_env_access.py's wrapped command invokes it (real stdin/stdout, one
positional argument). Every .env fixture here is synthetic, created under
tmp_path -- this file never reads or references the project's real .env."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "redact_output.py"


def _run(env_path: Path, stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(env_path)],
        input=stdin_text, capture_output=True, text=True,
    )


def test_redacts_a_secret_value_present_in_output(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("GITHUB_WEBHOOK_SECRET=abcdefgh12345678\n")
    result = _run(env_path, "line one\nabcdefgh12345678\nline three\n")
    assert result.returncode == 0
    assert "abcdefgh12345678" not in result.stdout
    assert "[REDACTED-SECRET]" in result.stdout


def test_leaves_values_below_minimum_length_alone(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("GROQ_KEY_SLOT=3\n")
    result = _run(env_path, "the slot is 3\n")
    assert result.returncode == 0
    assert result.stdout == "the slot is 3\n"


def test_redacts_a_multiline_secret_value(tmp_path):
    secret = "-----BEGIN KEY-----\nabcdefgh12345678\n-----END KEY-----"
    env_path = tmp_path / ".env"
    env_path.write_text(f'SOME_PEM="{secret}"\n')
    result = _run(env_path, f"before\n{secret}\nafter\n")
    assert result.returncode == 0
    assert secret not in result.stdout
    assert "[REDACTED-SECRET]" in result.stdout
    assert "before" in result.stdout and "after" in result.stdout


def test_passes_through_output_with_no_secrets(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("GITHUB_WEBHOOK_SECRET=abcdefgh12345678\n")
    result = _run(env_path, "totally unrelated output\n")
    assert result.returncode == 0
    assert result.stdout == "totally unrelated output\n"


def test_fails_closed_when_env_file_is_missing(tmp_path):
    env_path = tmp_path / "does-not-exist" / ".env"
    result = _run(env_path, "some output\n")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "failed to load secrets" in result.stderr


def test_fails_closed_with_wrong_argument_count():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input="some output\n", capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert result.stdout == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_redact_output_hook.py -v`
Expected: every test FAILs — `redact_output.py` doesn't exist yet
(`FileNotFoundError` / non-zero from `subprocess.run` finding no such file,
or an import/collection error depending on how pytest reports a missing
script target — either way, none should pass).

- [ ] **Step 3: Write `redact_output.py`**

Create `.claude/hooks/redact_output.py`:

```python
"""Output filter for `.env` secret redaction.

Invoked by check_env_access.py's wrapped Bash/PowerShell command -- never
by Claude Code directly. Reads a wrapped command's real combined
stdout+stderr from stdin, replaces every occurrence of a real `.env`
secret value with a fixed marker, and writes the result to stdout.

Fails closed: any internal error (missing/unreadable .env, wrong argument
count, unexpected exception) prints a diagnostic to stderr and exits 1
WITHOUT printing any of the buffered input. Combined with the wrapping
command's `set -o pipefail` (bash) / exit-code capture (PowerShell), this
makes the whole wrapped command surface as a failure rather than ever
emitting unredacted content.

See docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md for the
full design this implements.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values

_MIN_SECRET_LENGTH = 8
_MARKER = "[REDACTED-SECRET]"


def _secret_values(env_path: Path) -> list[str]:
    if not env_path.is_file():
        raise FileNotFoundError(f"{env_path} not found")
    values = dotenv_values(env_path)
    return [v for v in values.values() if v is not None and len(v) >= _MIN_SECRET_LENGTH]


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, _MARKER)
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "redact_output.py: expected exactly one argument, the .env path",
            file=sys.stderr,
        )
        return 1
    env_path = Path(argv[1])
    text = sys.stdin.read()
    try:
        secrets = _secret_values(env_path)
    except Exception as exc:  # noqa: BLE001 -- fail closed on ANY error
        print(f"redact_output.py: failed to load secrets for redaction: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(_redact(text, secrets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_redact_output_hook.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add .claude/hooks/redact_output.py tests/test_redact_output_hook.py
git commit -m "$(cat <<'EOF'
feat: add .env secret redaction filter for the PreToolUse hook

A content-based filter that replaces real .env secret values in a
wrapped command's output with a fixed marker, fails closed on any
internal error. Standalone script -- not yet wired into
check_env_access.py (next task).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ELV1aMVADFCru5BUd4kS1B
EOF
)"
```

---

### Task 2: Rewrite `check_env_access.py` to wrap shell commands instead of scanning them

**Files:**
- Modify: `.claude/hooks/check_env_access.py` (full rewrite of its shell-command
  handling; the path-field deny logic is preserved unchanged)
- Modify: `tests/test_check_env_access_hook.py` (full rewrite — the old
  substring-scan and git/gh-message-exemption tests no longer apply)

**Interfaces:**
- Consumes: `.claude/hooks/redact_output.py` (Task 1) — invoked by the
  command string this task constructs, at the path
  `Path(__file__).resolve().parent / "redact_output.py"` relative to
  `check_env_access.py`'s own location.
- Produces: for every `Bash`/`PowerShell` `PreToolUse` call, prints
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"command": <wrapped>}}}`
  where `<wrapped>` is the shell-specific wrapped command string. For a
  `Read`/`Edit`/`Write`/`NotebookEdit`/`Grep`/`Glob` call whose
  `file_path`/`path`/`notebook_path` matches `.env`, prints the existing
  deny JSON (`permissionDecision: "deny"`, unchanged `_REASON` text). Prints
  nothing for anything else (plain allow).

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/test_check_env_access_hook.py`:

```python
"""Exercises .claude/hooks/check_env_access.py via subprocess, mirroring the
exact exec-form invocation Claude Code's PreToolUse hook actually uses (no
shell, literal argv) -- see the script's own module docstring for what it
does and why. This is a repo-tooling script, not part of the pr-review-bot
package, so it's tested by invoking it directly rather than importing it."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "check_env_access.py"
_PROJECT_ROOT = _HOOK.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


def _run(tool_name: str, tool_input: dict) -> tuple[bool, str]:
    """(printed_anything, raw_stdout). The hook prints exactly one JSON line
    for a deny or an allow-with-rewrite, and nothing for a plain allow."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, check=True,
    )
    return bool(result.stdout.strip()), result.stdout.strip()


def _decision(out: str) -> dict:
    return json.loads(out)["hookSpecificOutput"]


# --- Structured tools: unchanged path-field deny ---

def test_blocks_read_of_env_by_path():
    printed, out = _run("Read", {"file_path": ".env"})
    assert printed and _decision(out)["permissionDecision"] == "deny"


def test_blocks_edit_targeting_env():
    printed, out = _run("Edit", {"file_path": ".env", "old_string": "x", "new_string": "y"})
    assert printed and _decision(out)["permissionDecision"] == "deny"


def test_blocks_write_targeting_env():
    printed, out = _run("Write", {"file_path": ".env", "content": "GITHUB_APP_ID=1"})
    assert printed and _decision(out)["permissionDecision"] == "deny"


def test_blocks_grep_path_pointed_at_env():
    printed, out = _run("Grep", {"pattern": "KEY", "path": ".env"})
    assert printed and _decision(out)["permissionDecision"] == "deny"


def test_allows_env_example():
    printed, _ = _run("Read", {"file_path": ".env.example"})
    assert not printed


def test_allows_env_config():
    printed, _ = _run("Edit", {"file_path": ".env.config", "old_string": "a", "new_string": "b"})
    assert not printed


def test_allows_unrelated_file_path():
    printed, _ = _run("Read", {"file_path": "config.py"})
    assert not printed


def test_allows_write_content_mentioning_env_in_prose():
    """Regression: an earlier version matched the whole payload, so writing
    documentation that talks about .env was blocked outright."""
    printed, _ = _run("Write", {
        "file_path": "guide/setup/02-github-app.md",
        "content": "Paste it into GITHUB_APP_ID in .env. cp .env.example .env first.",
    })
    assert not printed


def test_allows_edit_old_new_string_mentioning_env():
    printed, _ = _run("Edit", {
        "file_path": "CLAUDE.md",
        "old_string": "never touch .env",
        "new_string": "never touch .env, full stop",
    })
    assert not printed


def test_allows_grep_pattern_searching_for_the_literal_string():
    """Searching FOR the string ".env" across other files is not the same
    as operating on the real .env -- only `path` is checked, never `pattern`."""
    printed, _ = _run("Grep", {"pattern": r"\.env", "path": "guide/"})
    assert not printed


# --- Shell tools: every command is wrapped, none are denied ---

def _wrapped_command(tool_name: str, original: str) -> str:
    _, out = _run(tool_name, {"command": original})
    decision = _decision(out)
    assert decision["permissionDecision"] == "allow"
    return decision["updatedInput"]["command"]


def test_bash_command_is_rewritten_not_denied():
    command = _wrapped_command("Bash", "cat .env")
    assert "redact_output.py" in command
    assert str(_ENV_PATH) in command
    assert "cat .env" in command


def test_bash_wrap_preserves_original_command_verbatim():
    original = "grep -rn foo ."
    command = _wrapped_command("Bash", original)
    assert original in command


def test_bash_wrap_uses_pipefail_and_brace_group():
    command = _wrapped_command("Bash", "echo hi")
    assert command.startswith("set -o pipefail; { echo hi")
    assert "} 2>&1 |" in command


def test_bash_wrap_invokes_redact_script_via_no_project_directory():
    command = _wrapped_command("Bash", "echo hi")
    assert "uv run --no-project --directory" in command
    assert str(_PROJECT_ROOT) in command


def test_unrelated_bash_command_still_gets_wrapped():
    """Universal wrapping means even a command with nothing to do with .env
    still gets the redaction pipe -- that's what makes the false-negative
    problem structurally impossible instead of pattern-dependent."""
    command = _wrapped_command("Bash", "git status")
    assert "redact_output.py" in command


def test_commit_message_mentioning_env_and_a_mutation_verb_is_not_denied():
    """Regression for the false positive a closed-vocabulary mutation guard
    would have reintroduced: a commit message documenting this very
    feature, using words like 'chmod' and '.env' together in prose, must
    never be denied -- there is no command-shape detection left to trip
    on it."""
    command = 'git commit -m "the hook now denies chmod/rm targeting .env"'
    wrapped = _wrapped_command("Bash", command)
    assert command in wrapped


def test_a_real_mutating_command_is_wrapped_not_denied():
    """Mutation prevention is enforced by the filesystem (chmod 400 +
    chattr +i on .env, applied outside this hook), not by this hook -- so
    even `rm .env` gets wrapped like any other command. It fails with a
    normal OS permission error when it actually runs, which is harmless
    output that flows through the same redaction wrapper as anything else."""
    command = "rm .env"
    wrapped = _wrapped_command("Bash", command)
    assert command in wrapped


def test_powershell_command_is_rewritten_not_denied():
    _, out = _run("PowerShell", {"command": "Get-Content .env"})
    decision = _decision(out)
    assert decision["permissionDecision"] == "allow"
    wrapped = decision["updatedInput"]["command"]
    assert "Get-Content .env" in wrapped
    assert "redact_output.py" in wrapped
    assert "$LASTEXITCODE" in wrapped


def test_powershell_wrap_captures_original_exit_code_before_filtering():
    _, out = _run("PowerShell", {"command": "git status"})
    wrapped = _decision(out)["updatedInput"]["command"]
    assert "$__redact_code = $LASTEXITCODE" in wrapped
    assert "exit $__redact_code" in wrapped


# --- Malformed input ---

def test_malformed_json_fails_toward_checking_the_raw_payload():
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="not json but mentions .env anyway",
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip()
    assert _decision(result.stdout.strip())["permissionDecision"] == "deny"


def test_malformed_json_without_env_mention_is_silent():
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="not json at all",
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == ""


# --- The real configured invocation, not just the script directly ---

_SETTINGS = _PROJECT_ROOT / ".claude" / "settings.json"


def _configured_hook_command() -> list[str]:
    with _SETTINGS.open() as f:
        settings = json.load(f)
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    return [hook["command"], *hook["args"]]


def _run_via_configured_invocation(tool_name: str, tool_input: dict) -> str:
    command = [
        arg.replace("${CLAUDE_PROJECT_DIR}", str(_PROJECT_ROOT)) for arg in _configured_hook_command()
    ]
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    result = subprocess.run(
        command,
        input=payload, capture_output=True, text=True, cwd=_PROJECT_ROOT, check=True,
    )
    return result.stdout.strip()


def test_configured_invocation_blocks_env_by_path():
    out = _run_via_configured_invocation("Read", {"file_path": ".env"})
    assert _decision(out)["permissionDecision"] == "deny"


def test_configured_invocation_allows_env_example():
    assert _run_via_configured_invocation("Read", {"file_path": ".env.example"}) == ""


def test_configured_invocation_wraps_bash_commands():
    out = _run_via_configured_invocation("Bash", {"command": "echo hi"})
    decision = _decision(out)
    assert decision["permissionDecision"] == "allow"
    assert "redact_output.py" in decision["updatedInput"]["command"]


def test_configured_invocation_does_not_depend_on_project_sync():
    command = _configured_hook_command()
    assert "--no-project" in command
    assert "--no-sync" not in command


def test_configured_invocation_does_not_depend_on_session_cwd():
    command = _configured_hook_command()
    assert any("${CLAUDE_PROJECT_DIR}" in arg for arg in command)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_check_env_access_hook.py -v`
Expected: FAIL — the old `check_env_access.py` still denies Bash/PowerShell
commands mentioning `.env` instead of wrapping them (e.g.
`test_bash_command_is_rewritten_not_denied` fails because the hook prints a
`deny`, not an `allow`-with-`updatedInput`), and several new tests reference
behavior (`updatedInput`) that doesn't exist yet in the old script.

- [ ] **Step 3: Rewrite `check_env_access.py`**

Replace the full contents of `.claude/hooks/check_env_access.py`:

```python
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
    reflect the ORIGINAL command's success/failure, not the filter's,
    in the normal case (confirmed: `set -o pipefail; { false; } 2>&1 | cat`
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_check_env_access_hook.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full test suite and lint**

Run: `uv run pytest -v` and `uv run ruff check .`
Expected: both green. Also run:
`grep -rln "_neutralize_git_message_values\|_texts_to_check" --include="*.py" .`
Expected: no output — those names were private to the old
`check_env_access.py` and are now deleted; any match means something else
in the repo still depends on them and needs to be dealt with before
continuing.

- [ ] **Step 6: Commit**

```bash
git add .claude/hooks/check_env_access.py tests/test_check_env_access_hook.py
git commit -m "$(cat <<'EOF'
feat: rewrite check_env_access.py to redact output instead of scanning commands

Replaces the text-scanning approach (incomplete against commands that
never type ".env", imprecise against prose that merely mentions it)
with an unconditional PreToolUse rewrite: every Bash/PowerShell command
now pipes its real output through redact_output.py before Claude sees
it. Structured-tool path-field denial (Read/Edit/Write/NotebookEdit/
Grep/Glob) is unchanged. Mutation prevention moves out of the hook
entirely, onto the filesystem (chmod 400 + chattr +i on .env, applied
manually -- see the design doc).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ELV1aMVADFCru5BUd4kS1B
EOF
)"
```

---

### Task 3: End-to-end integration tests (real execution, not just JSON inspection)

**Files:**
- Create: `tests/test_env_hook_redaction_integration.py`

**Interfaces:**
- Consumes: `check_env_access.py` (Task 2, invoked via subprocess to obtain
  a real wrapped command) and `redact_output.py` (Task 1, invoked
  indirectly as part of that wrapped command when it's actually executed
  via `bash -c`).
- Produces: nothing consumed by later tasks — this is a verification-only
  task.

- [ ] **Step 1: Write the tests**

Create `tests/test_env_hook_redaction_integration.py`:

```python
"""End-to-end integration tests for the .env redaction wrapper: obtains the
actual wrapped command check_env_access.py produces, executes it for real
via bash, and checks the REAL observable behavior -- exit code fidelity and
actual redaction of a synthetic secret -- rather than just inspecting the
JSON the hook prints (that's covered by tests/test_check_env_access_hook.py).
Never touches the project's real .env; every fixture is synthetic, created
under tmp_path."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "check_env_access.py"
_REAL_ENV_PATH = _HOOK.parent.parent / ".env"


def _wrapped_bash_command(original: str, env_path: Path) -> str:
    """Gets check_env_access.py's real wrapping logic for `original`, then
    substitutes a synthetic env_path for the real project .env path inside
    the resulting command text -- the hook has no separate knob for "use a
    different .env", so this is the simplest way to reuse its exact
    wrapping behavior against a fixture instead of the real file."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": original}})
    result = subprocess.run(
        [sys.executable, str(_HOOK)], input=payload, capture_output=True, text=True, check=True,
    )
    decision = json.loads(result.stdout.strip())["hookSpecificOutput"]
    command = decision["updatedInput"]["command"]
    return command.replace(str(_REAL_ENV_PATH), str(env_path))


def test_exit_code_fidelity_for_a_succeeding_original_command(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_KEY=abcdefgh12345678\n")
    command = _wrapped_bash_command("true", env_path)
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode == 0


def test_exit_code_fidelity_for_a_failing_original_command(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_KEY=abcdefgh12345678\n")
    command = _wrapped_bash_command("false", env_path)
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode == 1


def test_grep_recursive_walk_into_env_comes_back_redacted(tmp_path):
    """Reproduces the exact ISSUES.md incident shape: a recursive grep that
    never names .env in its own arguments, but whose search directory
    contains one -- as a regression test, using a synthetic secret, never
    the real project .env."""
    (tmp_path / "notes.txt").write_text("scripts/foo.py handles widgets\n")
    env_path = tmp_path / ".env"
    env_path.write_text("GITHUB_WEBHOOK_SECRET=abcdefgh12345678\n")
    original = f"grep -rn 'widgets\\|SECRET' {shlex.quote(str(tmp_path))}"
    command = _wrapped_bash_command(original, env_path)
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert "abcdefgh12345678" not in result.stdout
    assert "[REDACTED-SECRET]" in result.stdout
    assert "widgets" in result.stdout


def test_a_command_that_never_mentions_env_at_all_still_gets_redacted(tmp_path):
    """The core false-negative fix: nothing about this command's text
    involves .env in any way, yet it still leaks the secret into its
    output (simulating some other accidental exposure path) -- and the
    universal wrapper catches it anyway, because redaction is content-based,
    not command-text-based."""
    env_path = tmp_path / ".env"
    env_path.write_text("GITHUB_WEBHOOK_SECRET=abcdefgh12345678\n")
    command = _wrapped_bash_command("echo abcdefgh12345678", env_path)
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert "abcdefgh12345678" not in result.stdout
    assert "[REDACTED-SECRET]" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail or pass appropriately**

Run: `uv run pytest tests/test_env_hook_redaction_integration.py -v`
Expected: PASS already, since Tasks 1 and 2 are complete at this point —
this task adds coverage at a different level (real execution) rather than
driving new implementation. If any test fails, that means the real,
end-to-end behavior diverges from what the unit-level tests in Tasks 1–2
already asserted — treat it as a genuine bug in either `redact_output.py`
or the wrap construction and fix the root cause before proceeding, not the
test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_env_hook_redaction_integration.py
git commit -m "$(cat <<'EOF'
test: add end-to-end integration tests for the .env redaction wrapper

Executes the real wrapped command via bash (not just inspecting the
hook's JSON output) to verify exit-code fidelity and reproduce the
exact ISSUES.md grep -rn incident shape as a regression test, using
synthetic .env fixtures throughout.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ELV1aMVADFCru5BUd4kS1B
EOF
)"
```

---

### Task 4: Close the ISSUES.md entry and log the residual Grep-tool gap

**Files:**
- Modify: `ISSUES.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by other tasks — this is the final task.

- [ ] **Step 1: Close the existing incident entry**

In `ISSUES.md`, find the entry titled `## Controller ran a directory-recursive
grep that swept in bot/.env without naming it` (its `**Suggested
CLAUDE.md/hook change:**` line currently ends with "...User's response: log
here now, revisit hardening the hook once the current fix/cleanup wave is
done. Until then, this session's own mitigation is to scope every subsequent
grep in this cleanup to explicit file lists or `--exclude=.env*`/`--include=`
rather than bare directories."). Append a new bullet immediately after it:

```markdown
- **Update (2026-09-06):** closed. `.claude/hooks/check_env_access.py` no
  longer scans shell-command text for `.env` at all — every `Bash`/
  `PowerShell` command is now unconditionally rewritten (via `PreToolUse`'s
  `updatedInput`) to pipe its real output through a new
  `.claude/hooks/redact_output.py` filter, which replaces any real `.env`
  secret value with `[REDACTED-SECRET]` before the result reaches Claude —
  content-based, so a recursive `grep`/`find`/anything that never types
  `.env` in its arguments is covered the same as one that does. See
  `docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md` for the
  full design and `tests/test_env_hook_redaction_integration.py`'s
  `test_grep_recursive_walk_into_env_comes_back_redacted` for a regression
  test reproducing this exact incident shape against a synthetic fixture.
  Mutation prevention (`rm`/`chmod`/etc. targeting `.env`) is handled
  separately, at the filesystem level (`chmod 400` + `chattr +i` on
  `.env`), applied manually by the user — not part of this fix.
```

- [ ] **Step 2: Log the residual Grep/Glob-tool gap as a new Parked Issue**

In `ISSUES.md`'s `## Parked Issues` section, add a new entry (following the
section's own documented format):

```markdown
### Claude Code's structured Grep/Glob tools aren't covered by the .env output-redaction wrapper
- **Found during:** implementing the .env-protection hook hardening
  (`docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md`).
- **What:** The redaction wrapper only rewrites `Bash`/`PowerShell` shell
  commands (via `updatedInput`) — it has no equivalent for Claude Code's
  own structured `Grep`/`Glob` tools, which don't execute a shell command
  at all. `check_env_access.py`'s path-field check still denies a `Grep`/
  `Glob` call whose `path` argument names `.env` exactly, but if either
  tool were pointed at a *directory* that merely contains `.env` (the
  structured-tool equivalent of the `grep -rn` incident this whole design
  fixes for the Bash case), a matched line from the real `.env` could
  still surface unredacted in the tool result.
- **Why parked:** Out of scope for this fix — the spec and this plan were
  both scoped to the shell-tool text-scanning problem specifically (the
  incident that motivated the work was a real `Bash` `grep -rn`, not the
  structured `Grep` tool), and this exact gap already existed, unaddressed,
  in the hook before this work started — not a regression introduced by
  it.
- **Follow-up:** Would need either (a) a `PreToolUse` check for `Grep`/
  `Glob` that determines whether the given `path`/glob would recursively
  include `.env` and denies if so (no rewrite mechanism exists for these
  tools' own output the way `updatedInput` exists for `Bash`'s `command`),
  or (b) confirming whether `updatedInput` can rewrite these tools' output
  post-hoc the way it rewrites `Bash`'s command pre-execution — needs its
  own design pass, not a quick add-on to this one.
```

- [ ] **Step 3: Commit**

```bash
git add ISSUES.md
git commit -m "$(cat <<'EOF'
docs: close the grep-into-.env ISSUES.md entry, log residual Grep-tool gap

The recursive-grep incident this entry describes is now fixed by the
redaction wrapper (content-based, not command-text-based). Logs the
one known remaining gap -- Claude Code's structured Grep/Glob tools
aren't covered by the wrapper -- as a Parked Issue, since it's out of
this fix's scope and pre-existed it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ELV1aMVADFCru5BUd4kS1B
EOF
)"
```

---

## Handoff note (not a task — action for the user, not Claude)

This plan does not apply `chmod 400` + `chattr +i` to the real `.env` — per
the design doc, that's a manual, out-of-band step, and `chattr -i`/`+i`
needs root, which Claude Code's Bash tool doesn't have (and shouldn't be
asked to sudo through). Once this plan is fully implemented, run, as
yourself, outside of Claude Code:

```bash
chmod 400 .env
sudo chattr +i .env
```

Remember that any future legitimate edit to `.env` will need
`sudo chattr -i .env` first and `sudo chattr +i .env` again afterward.
