"""Interactively scaffold .env and .env.config from the committed examples.

    uv run python -m scripts.init_env

HUMAN-RUN ONLY -- it prompts for and writes real credentials. An agent must
never invoke it (same rule as scripts/encode_credential.py).

It is idempotent, and does that WITHOUT ever reading an existing VALUE: to
decide whether a key is already set it reads key NAMES only, via the
'^[A-Z_0-9]+=' idiom CLAUDE.md prescribes (see tests/test_config.py's
_key_names for the same shape). Re-running is safe because every write is a
MERGE, not a replace: any key the operator chose to keep (answered "keep it?
[Y]") is passed through into the written file exactly as its line already
read, verbatim and unexamined -- see merge_env(). Only keys the operator typed
a fresh value for this run are changed. No existing secret is ever held in
memory by this script beyond that opaque line-level copy.

Which file a key belongs in comes from config.py's OPERATIONAL_KEYS:
listed = operational (.env.config), everything else = secret (.env). That is
the same split tests/test_config.py enforces, so this cannot drift from it.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

from pydantic import ValidationError

from config import OPERATIONAL_KEYS, Settings

# Captures the NAME and discards the value -- the whole point. A value may
# contain '=', spaces, quotes, or '#' and none of it can reach the result.
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z_0-9]+)=")

# Keys whose value is a file's base64 form rather than something typed.
_FILE_ENCODED_KEYS = frozenset({"GITHUB_APP_PRIVATE_KEY", "VERTEX_GCP_SERVICE_ACCOUNT_KEY"})


def key_names(path: Path) -> frozenset[str]:
    """The env-var NAMES defined in `path`; empty if it does not exist.

    Names only, never values. Handles CRLF, 'export ' prefixes, comments,
    blank lines, and values containing '=' -- because a regex that returns
    whole lines is exactly how a secret leaks (CLAUDE.md).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(
        match.group(1) for line in text.splitlines() if (match := _KEY_LINE.match(line))
    )


def example_keys(path: Path) -> tuple[str, ...]:
    """Every key an example file declares, in file order (commented-out
    optional settings included, so nothing silently goes unasked)."""
    text = path.read_text(encoding="utf-8")
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip().removeprefix("# ").removeprefix("#")
        match = _KEY_LINE.match(stripped)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return tuple(names)


def split_keys(keys: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(secret keys, operational keys). Secret-by-default: anything not on
    OPERATIONAL_KEYS is treated as a credential."""
    secret = tuple(k for k in keys if k not in OPERATIONAL_KEYS)
    operational = tuple(k for k in keys if k in OPERATIONAL_KEYS)
    return secret, operational


def render_env(values: dict[str, str]) -> str:
    return "".join(f"{name}={value}\n" for name, value in values.items())


def merge_env(existing_text: str, updates: dict[str, str]) -> str:
    """Merge `updates` into `existing_text`, line by line, without ever
    treating an existing value as anything but opaque text to pass through.

    For each line in `existing_text` that matches _KEY_LINE: if its key is in
    `updates`, the WHOLE line is replaced with the new `name=value` line and
    that key is consumed (so it is not appended again below); otherwise the
    line -- comment, blank, or an untouched key -- is kept verbatim, in place.
    Any keys still left in `updates` after every existing line is processed
    (i.e. brand-new keys not previously present) are appended at the end, in
    the order `updates` was given. This is how a "keep it? [Y]" answer for an
    already-set secret survives a re-run: this function never inspects that
    secret's value, only its line's key name.

    If `existing_text` is already malformed and defines the same key on two
    different lines, only the first occurrence is kept -- the stale duplicate
    is dropped rather than passed through, because a duplicate key surviving
    into the written file could shadow the fresh value under a dotenv-style
    "last occurrence wins" loader.
    """
    remaining = dict(updates)
    seen: set[str] = set()
    lines: list[str] = []
    for line in existing_text.splitlines():
        match = _KEY_LINE.match(line)
        name = match.group(1) if match else None
        if name is not None and name in seen:
            continue  # stale duplicate of a key already written above -- drop it
        if match and name in remaining:
            lines.append(f"{name}={remaining.pop(name)}")
        else:
            lines.append(line)
        if name is not None:
            seen.add(name)
    for name, value in remaining.items():
        lines.append(f"{name}={value}")
    return "".join(f"{line}\n" for line in lines)


def write_env(text: str, path: Path, overwrite: bool = False) -> None:
    """`path` is REQUIRED with no default, and an existing file is refused
    unless overwrite=True -- so neither a test nor a mis-run can destroy a
    working credential file."""
    if path.exists() and not overwrite:
        raise SystemExit(f"{path} already exists; pass overwrite=True to replace it.")
    path.write_text(text, encoding="utf-8", newline="\n")


def _format_error(name: str, value: str) -> str | None:
    """Structural-only description of why `value` fails Settings' own
    validation for this key, or None if it's fine (or the key isn't a plain
    Settings field, e.g. a numbered credential slot like GROQ_API_KEY_1,
    which is resolved dynamically rather than declared).

    Validates via Settings.model_validate({field: value}) -- pydantic's
    BaseModel entry point, not BaseSettings' __init__ -- so this checks ONLY
    the one field against its declared type/constraints, using every other
    field's default, with no .env/.env.config/process-env source consulted
    at all.

    Deliberately surfaces only each error's `msg` -- pydantic's default
    ValidationError text embeds the rejected value itself
    (`input_value=...`), which for a secret field would be exactly the
    un-redacted-exception leak CLAUDE.md's Secret handling section warns
    about, and this script's own contract is that a value is "never echoed
    back".
    """
    field = name.lower()
    if field not in Settings.model_fields:
        return None
    try:
        Settings.model_validate({field: value})
    except ValidationError as exc:
        messages = [err["msg"] for err in exc.errors() if err["loc"] == (field,)]
        return "; ".join(messages) if messages else "invalid value"
    return None


def _ask(name: str, already_set: bool) -> str | None:
    """Prompt for one key. None means 'leave it out of the written file'."""
    if already_set:
        keep = input(f"{name} is already set -- keep it? [Y/n] ").strip().lower()
        if keep in ("", "y", "yes"):
            return None
    if name in _FILE_ENCODED_KEYS:
        location = input(f"{name}: path to the key file (blank to skip): ").strip()
        if not location:
            return None
        try:
            return base64.b64encode(Path(location).read_bytes()).decode()
        except OSError as exc:
            print(f"could not read that file ({type(exc).__name__}); skipping", file=sys.stderr)
            return None
    while True:
        value = input(f"{name}: ").strip()
        if not value:
            return None
        error = _format_error(name, value)
        if error is None:
            return value
        print(f"{name}: {error} -- try again", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold .env and .env.config")
    parser.add_argument("--root", default=".", help="where the example files live")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.root)
    secret_path, config_path = root / ".env", root / ".env.config"
    declared = example_keys(root / ".env.example") + example_keys(root / ".env.config.example")
    secret_keys, operational_keys = split_keys(declared)
    existing = key_names(secret_path) | key_names(config_path)

    print("Values are written straight to .env / .env.config and never echoed back.\n")
    answers: dict[str, str] = {}
    for name in (*secret_keys, *operational_keys):
        value = _ask(name, name in existing)
        if value is not None:
            answers[name] = value

    for path, keys in ((secret_path, secret_keys), (config_path, operational_keys)):
        chosen = {k: v for k, v in answers.items() if k in keys}
        if not chosen:
            continue
        existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        merged = merge_env(existing_text, chosen)
        write_env(merged, path, overwrite=True)
        print(f"wrote {path} ({len(chosen)} keys)")

    print("next: uv run python -m scripts.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
