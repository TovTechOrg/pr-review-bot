"""Every probe returns names, lengths, or booleans -- never a value.

The types are the primary guarantee (frozenset[str] / dict[str, int] / bool
have nowhere to put a secret). These tests defend the layer the types cannot:
that nothing stringifies a value into a message, a traceback, or JSON on the
way out. See CLAUDE.md's "Secret handling" section, which this module exists
to make structurally enforceable rather than a matter of discipline.
"""
from __future__ import annotations

import json

import pytest

from config import settings
from scripts import _probes

# Distinctive, high-entropy, and structurally unlike a length or a name, so a
# substring search cannot pass by accident.
SENTINEL = "SENTINEL-7f3a91c4e5b8d206-DO-NOT-LEAK"


@pytest.fixture
def seeded(monkeypatch):
    """Every probed secret set to a unique sentinel value."""
    values = {}
    for name in _probes.PROBED_SECRETS:
        value = f"{SENTINEL}-{name}"
        values[name] = value
        monkeypatch.setattr(settings, name.lower(), value, raising=False)
    return values


def test_present_secrets_returns_names_only(seeded):
    result = _probes.present_secrets()
    assert isinstance(result, frozenset)
    assert "GITHUB_WEBHOOK_SECRET" in result
    for name in result:
        assert name in _probes.PROBED_SECRETS
        assert SENTINEL not in name


def test_secret_lengths_returns_integers_only(seeded):
    lengths = _probes.secret_lengths()
    assert lengths, "negative control: the probe must return something"
    for name, length in lengths.items():
        assert isinstance(length, int)
        assert length == len(seeded[name])


def test_no_probe_output_contains_any_sentinel(seeded, capsys):
    """The whole surface at once: return values, their repr, a JSON dump, and
    anything printed. A leak through any one of these is a leak."""
    payload = {
        "present": sorted(_probes.present_secrets()),
        "lengths": _probes.secret_lengths(),
        "pem_ok": _probes.private_key_decodes(),
    }
    surfaces = [repr(payload), json.dumps(payload), capsys.readouterr().out]
    for surface in surfaces:
        assert SENTINEL not in surface


def test_a_validation_failure_does_not_echo_the_value(monkeypatch):
    """pydantic's ValidationError echoes input_value, so a probe that lets one
    escape turns the error text itself into a secret leak (CLAUDE.md)."""
    monkeypatch.setattr(settings, "github_app_private_key", SENTINEL, raising=False)
    # A non-base64 value must be reported structurally, not by echoing it.
    assert _probes.private_key_decodes() is False
    try:
        _probes.secret_lengths()
    except Exception as exc:  # pragma: no cover -- must not raise at all
        pytest.fail(f"probe raised instead of degrading: {type(exc).__name__}")


def test_private_key_decodes_recognises_a_real_pem(monkeypatch):
    import base64

    pem = b"-----BEGIN RSA PRIVATE KEY-----\nZm9v\n-----END RSA PRIVATE KEY-----\n"
    monkeypatch.setattr(
        settings, "github_app_private_key", base64.b64encode(pem).decode(), raising=False
    )
    assert _probes.private_key_decodes() is True
