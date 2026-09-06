"""Output filter for `.env` secret redaction.

Invoked by check_env_access.py's wrapped Bash/PowerShell command -- never
by Claude Code directly. Reads a wrapped command's real combined
stdout+stderr from stdin, replaces every occurrence of a real `.env`
secret value with a fixed marker, and writes the result to stdout.

Fails closed: any internal error (missing/unreadable .env, wrong
argument count, unexpected exception) prints a diagnostic to stderr and
exits 1 WITHOUT printing any of the buffered input. Combined with the
wrapping command's `set -o pipefail` (bash) / exit-code capture
(PowerShell), this makes the whole wrapped command surface as a failure
rather than ever emitting unredacted content.

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
