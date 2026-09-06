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
_REAL_ENV_PATH = _HOOK.parent.parent.parent / ".env"


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
