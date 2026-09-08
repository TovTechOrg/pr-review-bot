"""Prints a local file's base64 form -- for pasting into GITHUB_APP_PRIVATE_KEY
or VERTEX_GCP_SERVICE_ACCOUNT_KEY[_n] in .env.

    uv run python -m scripts.encode_credential path/to/file.pem

Human-run only. An agent must never invoke this against a real credential
file: doing so would print secret-derived bytes into its own tool output --
exactly the failure mode CLAUDE.md's Secret handling section exists to
prevent. This script's existence does not change who is allowed to run it
against real material; only the user, in their own terminal.

Works identically for a PEM or a JSON key -- base64 doesn't care about
content shape. Equivalent to `base64 -w0 < file`, wrapped for convenience.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: encode_credential.py <path>", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"could not read {path}: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(base64.b64encode(data).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
