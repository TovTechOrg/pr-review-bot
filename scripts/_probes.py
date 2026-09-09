"""Secret-safe local state probes for scripts/doctor.py.

WHY THIS IS ITS OWN MODULE. Every function here returns a name, a count, or a
boolean, and the return TYPES (frozenset[str], dict[str, int], bool) leave
nowhere for a secret value to travel. That is the guarantee, and keeping it in
one small file is what makes it auditable -- see CLAUDE.md's "Secret handling"
section, and scripts/_render.py::env_vars()'s matching contract.

It reads individual Settings FIELDS and reduces each to a length or a boolean
at the point of access. It never reads .env as text: pydantic-settings already
parses both files, and text-parsing would reintroduce every case a regex gets
wrong (an '=' inside a DATABASE_URL, an unencoded multi-line PEM, 'export KEY=',
trailing comments, CRLF line endings from a Windows-authored file).

Nothing here raises. A probe's job is to report state; a probe that throws
would take out the very tool an operator is running to find out what is wrong.
"""

from __future__ import annotations

import base64
import binascii

from config import settings
from providers import registry

# Every secret-bearing env var doctor reports on. Enumerated literally rather
# than derived from a prefix, for the same reason config.py's
# OPERATIONAL_KEYS is: a pattern would silently pick up future names.
PROBED_SECRETS: tuple[str, ...] = (
    "GITHUB_APP_ID",  # not itself a secret, but doctor.py needs to know if it's configured
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "DATABASE_URL",
    *sorted({credential for credential, _model in registry.PROVIDERS.values()}),
)


def _raw(name: str) -> str:
    """The value of one Settings field as text, or '' -- callers MUST reduce
    this to a length or boolean immediately and never let it escape."""
    value = getattr(settings, name.lower(), "")
    if not value:
        return ""
    return str(value)


def present_secrets() -> frozenset[str]:
    """Names of probed secrets that have a non-empty value. Names only."""
    return frozenset(name for name in PROBED_SECRETS if _raw(name))


def secret_lengths() -> dict[str, int]:
    """name -> character count, for present secrets only. Counts only."""
    return {name: len(_raw(name)) for name in PROBED_SECRETS if _raw(name)}


def private_key_decodes() -> bool:
    """Whether GITHUB_APP_PRIVATE_KEY base64-decodes to PEM-shaped bytes.

    A boolean, deliberately: this is the one probe that must look at decoded
    secret material, so the decoded bytes never leave this function's frame.
    The most common setup mistake is pasting the PEM verbatim instead of its
    base64 form, which this catches without anyone seeing either.
    """
    raw = _raw("GITHUB_APP_PRIVATE_KEY")
    if not raw:
        return False
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return False
    return decoded.lstrip().startswith(b"-----BEGIN")
