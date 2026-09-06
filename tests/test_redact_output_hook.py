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
