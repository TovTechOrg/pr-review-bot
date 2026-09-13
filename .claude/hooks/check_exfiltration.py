"""PreToolUse hook: refuses to let a protected file's bytes leave this machine.

Complements `check_env_access.py`, which faces INBOUND -- it denies a tool call
naming `.env`, and rewrites every shell command so its output is scrubbed before
reaching Claude. This hook faces OUTBOUND. A command that *sends* a protected
file somewhere returns nothing interesting, so the redaction filter dutifully
scrubs a response that never contained the secret in the first place.

Deny is exit code 2, not JSON. `check_env_access.py` returns
permissionDecision "allow" for every Bash command, and exit 2 is the documented
way to stop a call regardless of a sibling hook's allow. Ask has no exit-code
form and must use JSON -- see the design doc's Open risk 1.

This file is BYTE-IDENTICAL in ~/pr-review-bot and ~/onboarding-wizard. Any
change must be ported to the other repo in the same session and verified with
`diff`. See docs/superpowers/specs/2026-09-13-exfiltration-guard-design.md.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _HOOK_DIR.parent.parent

# ===========================================================================
# PROTECTED PATHS -- edit this block to guard a new location, or stop
# guarding one. This is the only part of this file you should need to touch.
#
# Each entry is ("KIND", "path"). Three kinds are understood:
#
#   "REPO"  path relative to the repository root
#   "HOME"  path relative to the current user's home directory
#   "GLOB"  a filename pattern, matched wherever the file happens to live
#
# A trailing "/" marks a directory: everything beneath it is guarded too.
#
#   To ADD a location ...... append one line to the right group below
#   To STOP guarding one ... delete its line; nothing else references it
#   After either .......... uv run pytest tests/test_exfiltration_hook.py
#
# Examples (copy one, edit it, drop the leading "#"):
#
#   ("REPO", ".env")                        one file in the repo
#   ("REPO", "secrets/")                    a whole directory in the repo
#   ("HOME", ".config/impeccable-hf/token") one file under ~
#   ("HOME", ".ssh/")                       a whole directory under ~
#   ("GLOB", "*.pem")                       any .pem, anywhere
#   ("GLOB", "*service-account*.json")      any file matching, anywhere
#
# NOTE: listing a path here does NOT change who can read it. This list only
# controls what this guard refuses to send OUTWARD. File permissions are a
# separate mechanism -- see CLAUDE.md's "Secret handling" section.
# ===========================================================================
_PROTECTED: tuple[tuple[str, str], ...] = (
    # --- this repo's own credentials ("REPO" resolves per repo, so this one
    #     line is correct in pr-review-bot AND onboarding-wizard) ---
    ("REPO", ".env"),

    # --- redaction-wrapper sinks: unredacted command output at rest.
    #     BOTH repos' sinks are listed in BOTH copies on purpose. A path that
    #     does not exist simply never matches, and keeping the entry here is
    #     what lets this file stay byte-identical across the two repos. The
    #     wizard's does not exist yet -- its wrapper still pipes. ---
    ("HOME", ".cache/pr-review-bot-redact/"),
    ("HOME", ".cache/onboarding-wizard-redact/"),

    # --- machine-local credentials owned by other tooling ---
    ("HOME", ".config/impeccable-hf/token"),
    ("HOME", ".ssh/"),
    ("HOME", ".config/gh/"),

    # --- key material that can land anywhere ---
    ("GLOB", "*.pem"),
    ("GLOB", "*service-account*.json"),
)


def _target(kind: str, raw: str) -> tuple[Path, bool]:
    """(absolute path, is_directory_prefix) for one non-GLOB entry."""
    base = _PROJECT_ROOT if kind == "REPO" else Path.home()
    return base / raw.rstrip("/"), raw.endswith("/")


def _candidate_paths(token: str) -> list[str]:
    """Every path-ish string a single shell token might be carrying.

    One token can hide a path behind a payload sigil (`@.env`), a joined flag
    (`--body-file=.env`), an unspaced redirect (`<.env`), or a payload flag
    glued directly to its value with no separator at all -- real curl parsing
    behavior for short options (`-d@.env`, `-T.env`, `-Fname=@.env`) that a
    space- or `=`-only check misses entirely, plus the same shape defensively
    for long options (`--data@.env`). Each is unwrapped here so the caller
    only ever reasons about plain paths.
    """
    stripped = token.lstrip("<")
    stripped = stripped[1:] if stripped.startswith("@") else stripped
    out = [stripped]
    if "=" in stripped:
        out.append(stripped.split("=", 1)[1].lstrip("@"))
    glued = _glued_payload_value(token)
    if glued:
        out.append(glued)
    return [value for value in out if value]


def _names_protected(candidate: str, cwd: Path) -> bool:
    """True if `candidate` resolves to a protected path.

    Deliberately does NOT require the file to exist: a command naming a
    protected path that is currently absent is exactly as suspicious, and the
    wizard's reserved sink entry never exists at all.
    """
    try:
        expanded = Path(candidate).expanduser()
        absolute = expanded if expanded.is_absolute() else cwd / expanded
        norm = Path(os.path.normpath(str(absolute)))
    except (OSError, ValueError, RuntimeError):
        return False
    for kind, raw in _PROTECTED:
        if kind == "GLOB":
            if fnmatch.fnmatch(norm.name, raw):
                return True
            continue
        target, is_prefix = _target(kind, raw)
        target = Path(os.path.normpath(str(target)))
        if norm == target or (is_prefix and target in norm.parents):
            return True
    return False


_SHELL_TOOLS = ("Bash", "PowerShell")
_SEPARATORS = frozenset({"|", "||", "&&", ";", "&"})

# Flags whose value is a file whose CONTENTS become the request body.
_PAYLOAD_FLAGS = frozenset({
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "-F", "--form", "-T", "--upload-file",
    "--body-file", "--post-file", "--file",
})


def _glued_payload_value(token: str) -> str | None:
    """If `token` is a payload flag glued directly to its value with no
    space and no `=`, return that value; otherwise None.

    curl's short options (`-d`, `-F`, `-T`) attach their value directly with
    no separator at all (`-d@.env`, `-T.env`, `-Fname=@.env`) -- this is
    ordinary getopt-style short-option parsing, not an edge case, so any
    remainder after a short flag is a candidate. Long options only glue this
    way defensively here (`--data@.env`): real curl requires `=` for those,
    but catching the `@`-prefixed shape costs nothing since it can't fire on
    an ordinary long flag's unrelated argument.
    """
    for flag in _PAYLOAD_FLAGS:
        if not token.startswith(flag) or token == flag:
            continue
        remainder = token[len(flag):]
        is_short = len(flag) == 2
        if is_short or remainder.startswith("@"):
            return remainder.split("=", 1)[-1].lstrip("@")
    return None

_NETWORK_VERBS = frozenset({
    "curl", "wget", "nc", "netcat", "ncat", "scp", "rsync", "ssh", "sftp",
    "ftp", "telnet", "gh", "docker", "aws", "gcloud", "az", "http", "httpie",
})

# Commands that read a file's bytes onto stdout, where they can be piped on.
_READER_VERBS = frozenset({
    "cat", "base64", "tar", "gzip", "zip", "xxd", "od", "strings", "head", "tail",
})

_ARCHIVE_VERBS = frozenset({"tar", "zip", "gzip", "7z"})

_REMOTE_TARGET_RE = re.compile(r"^[^/\s]*@?[^/\s]*:")


def _tokenize(command: str) -> list[str]:
    """Best-effort shell tokenization. Falls back to whitespace splitting on
    unbalanced quotes rather than refusing to inspect the command at all."""
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def _command_heads(tokens: list[str]) -> list[str]:
    """Basenames of every token in command-name position: the first token, and
    the first after each separator. `|`-joined stages each contribute one."""
    heads: list[str] = []
    expect_head = True
    for token in tokens:
        if token in _SEPARATORS:
            expect_head = True
            continue
        if expect_head:
            heads.append(Path(token).name)
            expect_head = False
    return heads


def _protected_hits(tokens: list[str], cwd: Path) -> list[int]:
    return [
        index
        for index, token in enumerate(tokens)
        if any(_names_protected(candidate, cwd) for candidate in _candidate_paths(token))
    ]


def _deny_reason(tokens: list[str], cwd: Path) -> str | None:
    """A protected path in a position that has no benign reading."""
    hits = _protected_hits(tokens, cwd)
    if not hits:
        return None
    heads = _command_heads(tokens)

    for index in hits:
        token = tokens[index]
        previous = tokens[index - 1] if index else ""
        flag = token.split("=", 1)[0] if token.startswith("-") and "=" in token else ""

        if previous in _PAYLOAD_FLAGS or flag in _PAYLOAD_FLAGS or _glued_payload_value(token):
            return f"{token} is a protected path passed as a request payload"
        if token.startswith("@"):
            return f"{token} sends a protected file's contents as a payload"
        if previous == "<" or token.startswith("<"):
            return f"{token} feeds a protected file into a command on stdin"
        if heads and heads[0] in {"scp", "rsync"}:
            if any(_REMOTE_TARGET_RE.match(later) for later in tokens[index + 1:]):
                return f"{token} is a protected path being copied to a remote host"

    if "git" in heads and "add" in tokens and {"-f", "--force"} & set(tokens):
        return "force-staging a protected path into git"

    if any(head in _READER_VERBS for head in heads) and any(
        head in _NETWORK_VERBS for head in heads
    ):
        return "a protected path is read into a pipeline that reaches the network"

    return None


def _docker_context_reason(tokens: list[str], heads: list[str], cwd: Path) -> str | None:
    """`docker build`'s context directory can hold a protected repo-relative
    path even when the command line never names one directly -- the
    `.dockerignore` lines are a second net, not the first (design doc, Open
    risk 3). Checks the context directory's own contents (an existence
    check, never a read) rather than the command-line tokens."""
    if "docker" not in heads or "build" not in tokens:
        return None
    positional = [t for t in tokens[1:] if not t.startswith("-")]
    context = positional[-1] if positional else "."
    try:
        context_dir = Path(context)
        context_dir = context_dir if context_dir.is_absolute() else cwd / context_dir
        context_dir = Path(os.path.normpath(str(context_dir)))
    except (OSError, ValueError, RuntimeError):
        return None
    if not context_dir.is_dir():
        return None
    for kind, raw in _PROTECTED:
        if kind == "REPO" and (context_dir / raw.rstrip("/")).exists():
            return f"the docker build context ({context_dir}) contains {raw}"
    return None


def _ask_reason(tokens: list[str], cwd: Path) -> str | None:
    """A protected path plus an outward-facing verb, in a shape that does not
    prove intent. Ask rather than deny: this project runs gh/curl/docker
    constantly for legitimate reasons."""
    heads = _command_heads(tokens)
    if _protected_hits(tokens, cwd):
        if any(head in _NETWORK_VERBS for head in heads):
            return "this command names a protected path and can reach the network"
        if any(head in _ARCHIVE_VERBS for head in heads):
            return "this command packs a protected path into an archive"
        if "|" in tokens and any(head in _READER_VERBS for head in heads):
            return "a protected path is piped into a program this guard does not recognize"
    return _docker_context_reason(tokens, heads, cwd)


def _ask(reason: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"Exfiltration guard: {reason}.",
        }
    })


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input") or {}
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Inbound protection is check_env_access.py's job and it fails toward
        # checking. Here a malformed payload carries no command to inspect, so
        # there is nothing to block; stay silent rather than blocking everything.
        return 0

    if tool_name not in _SHELL_TOOLS:
        return 0
    command = str(tool_input.get("command", ""))
    if not command:
        return 0

    cwd = Path(data.get("cwd") or _PROJECT_ROOT)
    tokens = _tokenize(command)

    reason = _deny_reason(tokens, cwd)
    if reason:
        print(
            f"Blocked by the exfiltration guard: {reason}. CLAUDE.md's Secret "
            "handling section forbids a secret value reaching anything that "
            "leaves this local session. If this is legitimate, ask the user to "
            "run it themselves.",
            file=sys.stderr,
        )
        return 2

    reason = _ask_reason(tokens, cwd)
    if reason:
        print(_ask(reason))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
