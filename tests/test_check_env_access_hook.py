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
_PROJECT_ROOT = _HOOK.parent.parent.parent
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
        arg.replace("${CLAUDE_PROJECT_DIR}", str(_PROJECT_ROOT))
        for arg in _configured_hook_command()
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
