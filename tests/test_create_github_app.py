"""The manifest is the security boundary of this whole setup path, so its
shape is asserted rather than assumed. Every test writes to tmp_path only --
never the repo's real .env."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

import httpx
import pytest
import respx

from scripts import create_github_app as cga

SENTINEL_SECRET = "SENTINEL-9c1e4b7a60df2358-WEBHOOK"
SENTINEL_PEM = "-----BEGIN RSA PRIVATE KEY-----\nSENTINEL-PEM-BODY\n-----END RSA PRIVATE KEY-----\n"


def test_manifest_requests_exactly_the_documented_permissions():
    manifest = cga.build_manifest("bot", "https://example.com", "http://localhost:8765/callback")
    assert manifest["default_permissions"] == {
        "pull_requests": "write",
        "contents": "read",
        "issues": "write",
        "metadata": "read",
    }
    assert manifest["default_events"] == ["pull_request"]


def test_manifest_keeps_the_app_private():
    """A PUBLIC App lets any third party self-install and have their events
    accepted while GITHUB_TARGET_REPO=* (track-all mode) --
    guide/setup/02-github-app.md records this as the reason the App must
    stay private."""
    assert cga.build_manifest("bot", "https://e.com", "http://localhost:1/c")["public"] is False


def test_manifest_points_the_hook_at_the_webhook_path():
    manifest = cga.build_manifest("bot", "https://e.com", "http://localhost:1/c")
    assert manifest["hook_attributes"]["url"] == "https://e.com/webhook"
    assert manifest["redirect_url"] == "http://localhost:1/c"


def test_manifest_is_json_serialisable():
    """It is submitted as a form field, so it must round-trip through JSON."""
    json.loads(json.dumps(cga.build_manifest("bot", "https://e.com", "http://l/c")))


def test_exchange_code_parses_githubs_conversion_response():
    with respx.mock:
        respx.post("https://api.github.com/app-manifests/CODE123/conversions").mock(
            return_value=httpx.Response(
                201,
                json={"id": 4242, "pem": SENTINEL_PEM, "webhook_secret": SENTINEL_SECRET},
            )
        )
        creds = cga.exchange_code("CODE123")
    assert creds.app_id == 4242
    assert creds.private_key_pem == SENTINEL_PEM
    assert creds.webhook_secret == SENTINEL_SECRET


def test_exchange_code_reports_a_failure_structurally(capsys):
    """A 4xx body can echo request content; report the status, not the body."""
    with respx.mock:
        respx.post("https://api.github.com/app-manifests/BAD/conversions").mock(
            return_value=httpx.Response(422, json={"message": SENTINEL_SECRET})
        )
        with pytest.raises(SystemExit) as exc:
            cga.exchange_code("BAD")
    assert SENTINEL_SECRET not in str(exc.value)
    assert SENTINEL_SECRET not in capsys.readouterr().err


def test_write_credentials_writes_values_but_reports_only_lengths(tmp_path, capsys):
    env = tmp_path / ".env"
    creds = cga.AppCredentials(app_id=4242, private_key_pem=SENTINEL_PEM,
                               webhook_secret=SENTINEL_SECRET)
    reported = cga.write_credentials(creds, env)

    written = env.read_text(encoding="utf-8")
    assert "GITHUB_APP_ID=4242" in written
    assert SENTINEL_SECRET in written, "the file is the point -- it must carry the value"
    encoded = base64.b64encode(SENTINEL_PEM.encode()).decode()
    assert f"GITHUB_APP_PRIVATE_KEY={encoded}" in written, "PEM stored base64, not verbatim"

    assert set(reported) == {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"}
    assert all(isinstance(v, int) for v in reported.values())
    out = capsys.readouterr()
    for surface in (repr(reported), out.out, out.err):
        assert SENTINEL_SECRET not in surface
        assert "SENTINEL-PEM-BODY" not in surface


def test_write_credentials_refuses_to_clobber_an_existing_file(tmp_path):
    """A rerun must never silently destroy a working .env."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_APP_ID=1\n", encoding="utf-8")
    creds = cga.AppCredentials(1, SENTINEL_PEM, SENTINEL_SECRET)
    with pytest.raises(SystemExit):
        cga.write_credentials(creds, env)
    assert env.read_text(encoding="utf-8") == "GITHUB_APP_ID=1\n"
    cga.write_credentials(creds, env, overwrite=True)  # explicit opt-in works


def test_no_default_path_points_at_the_repo_root():
    """write_credentials must require an explicit path, so no test or misfire
    can land on the real .env."""
    import inspect

    signature = inspect.signature(cga.write_credentials)
    assert signature.parameters["path"].default is inspect.Parameter.empty


def test_main_reports_a_clear_message_when_the_callback_port_is_taken(monkeypatch, capsys):
    """A bare OSError (e.g. 'Address already in use') must not surface as an
    unhandled traceback -- main() should return 1 with an operator-friendly
    message, and never even reach the browser-opening step."""

    def _raise(*_args, **_kwargs):
        raise OSError("Address already in use")

    monkeypatch.setattr(cga.http.server, "HTTPServer", _raise)
    monkeypatch.setattr(
        cga.webbrowser, "open",
        lambda *_a, **_k: pytest.fail("must not open a browser after a bind failure"),
    )

    result = cga.main(["--base-url", "https://e.com"])

    assert result == 1
    err = capsys.readouterr().err
    assert str(cga._CALLBACK_PORT) in err
    assert "OSError" in err


def test_do_get_state_mismatch_sets_the_flag_and_leaves_code_none():
    """On a state mismatch, do_GET must record WHY (state_mismatch=True) and
    leave code None, so main() can report the real reason instead of the
    generic 'no code' message."""
    cga._CallbackHandler.state = "expected-state"
    cga._CallbackHandler.code = None
    cga._CallbackHandler.state_mismatch = False
    cga._CallbackHandler.received.clear()

    handler = cga._CallbackHandler.__new__(cga._CallbackHandler)
    handler.path = "/callback?state=wrong&code=abc123"
    replies: list[tuple[int, str]] = []
    handler._reply = lambda status, body: replies.append((status, body))

    handler.do_GET()

    assert cga._CallbackHandler.state_mismatch is True
    assert cga._CallbackHandler.code is None
    assert cga._CallbackHandler.received.is_set()
    assert replies and replies[0][0] == 400


def test_do_get_matching_state_clears_the_mismatch_flag_and_sets_code():
    cga._CallbackHandler.state = "expected-state"
    cga._CallbackHandler.code = None
    cga._CallbackHandler.state_mismatch = False
    cga._CallbackHandler.received.clear()

    handler = cga._CallbackHandler.__new__(cga._CallbackHandler)
    handler.path = "/callback?state=expected-state&code=abc123"
    handler._reply = lambda status, body: None

    handler.do_GET()

    assert cga._CallbackHandler.code == "abc123"
    assert cga._CallbackHandler.state_mismatch is False


def test_parse_redirect_code_from_a_full_url_with_matching_state():
    url = "https://example.invalid/callback?code=abc123&state=st1"
    assert cga.parse_redirect_code(url, "st1") == "abc123"


def test_parse_redirect_code_accepts_a_bare_code_with_no_state_check():
    """No '://' means it isn't a URL -- treat the whole thing as the code
    itself, same as pasting straight from a terminal that only showed the
    code (state verification is best-effort, not a hard requirement here)."""
    assert cga.parse_redirect_code("  abc123  ", "st1") == "abc123"


def test_parse_redirect_code_rejects_a_state_mismatch():
    url = "https://example.invalid/callback?code=abc123&state=wrong"
    with pytest.raises(SystemExit):
        cga.parse_redirect_code(url, "st1")


def test_parse_redirect_code_rejects_a_url_missing_the_code():
    url = "https://example.invalid/callback?state=st1"
    with pytest.raises(SystemExit):
        cga.parse_redirect_code(url, "st1")


def test_parse_redirect_code_rejects_empty_input():
    with pytest.raises(SystemExit):
        cga.parse_redirect_code("   ", "st1")


def test_manual_flow_uses_a_redirect_url_that_can_never_resolve():
    """--manual has no listener anywhere, so the redirect target only needs
    to survive in the browser's address bar -- it must never accidentally
    resolve to something real."""
    assert cga._MANUAL_REDIRECT.startswith("https://example.invalid/")


def test_manual_flow_writes_a_manifest_file_and_returns_the_pasted_code(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: "https://example.invalid/callback?code=CODE123&state=expected-state",
    )

    code = cga._run_manual_flow(
        "bot", "https://e.com", "expected-state", tmp_dir=tmp_path
    )

    assert code == "CODE123"
    written = list(tmp_path.glob("github-app-manifest-*.html"))
    assert len(written) == 1
    page = written[0].read_text(encoding="utf-8")
    assert cga._MANUAL_REDIRECT in page
    assert "document.forms[0].submit()" in page
    out = capsys.readouterr().out
    assert str(written[0]) in out


def test_manual_flow_never_starts_a_server_or_opens_a_browser(monkeypatch, tmp_path):
    """The whole point of --manual is zero localhost dependency -- it must
    never touch http.server or webbrowser at all."""
    monkeypatch.setattr(
        cga.http.server, "HTTPServer",
        lambda *_a, **_k: pytest.fail("must not start a callback server"),
    )
    monkeypatch.setattr(
        cga.webbrowser, "open",
        lambda *_a, **_k: pytest.fail("must not auto-open a browser"),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: "https://example.invalid/callback?code=CODE123&state=st",
    )

    assert cga._run_manual_flow("bot", "https://e.com", "st", tmp_dir=tmp_path) == "CODE123"


def test_manual_flow_reports_cancelled_on_eof(monkeypatch, tmp_path, capsys):
    def _raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    code = cga._run_manual_flow("bot", "https://e.com", "st", tmp_dir=tmp_path)

    assert code is None
    assert "cancelled" in capsys.readouterr().err


def test_manual_flow_propagates_a_bad_paste_as_a_clear_failure(monkeypatch, tmp_path):
    """A state mismatch on the pasted URL must fail the same structural way
    exchange_code() does -- SystemExit, not an unhandled KeyError."""
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: "https://example.invalid/callback?code=abc&state=WRONG",
    )
    with pytest.raises(SystemExit):
        cga._run_manual_flow("bot", "https://e.com", "expected-state", tmp_dir=tmp_path)


def test_server_flow_hints_at_manual_when_no_browser_opens(monkeypatch, capsys):
    """webbrowser.open() returning False -- exactly what happens with no
    browser controller, e.g. over SSH -- must print --manual as the next
    step immediately, not leave the operator waiting out the full timeout
    with no clue why."""

    class _FakeServer:
        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    class _FakeEvent:
        def clear(self):
            pass

        def wait(self, timeout=None):
            cga._CallbackHandler.code = "abc123"
            cga._CallbackHandler.state_mismatch = False
            return True

    monkeypatch.setattr(cga.http.server, "HTTPServer", lambda *a, **k: _FakeServer())
    monkeypatch.setattr(
        cga.threading, "Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    monkeypatch.setattr(cga.webbrowser, "open", lambda *_a, **_k: False)
    monkeypatch.setattr(cga._CallbackHandler, "received", _FakeEvent())

    code = cga._run_server_flow("bot", "https://e.com", "expected-state")

    assert code == "abc123"
    assert "--manual" in capsys.readouterr().err


def test_server_flow_timeout_hints_at_manual(monkeypatch, capsys):
    class _FakeServer:
        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    class _FakeEvent:
        def clear(self):
            pass

        def wait(self, timeout=None):
            return False

    monkeypatch.setattr(cga.http.server, "HTTPServer", lambda *a, **k: _FakeServer())
    monkeypatch.setattr(
        cga.threading, "Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    monkeypatch.setattr(cga.webbrowser, "open", lambda *_a, **_k: True)
    monkeypatch.setattr(cga._CallbackHandler, "received", _FakeEvent())

    code = cga._run_server_flow("bot", "https://e.com", "expected-state")

    assert code is None
    err = capsys.readouterr().err
    assert "--manual" in err
    assert "timed out" in err


def test_main_manual_flow_end_to_end_writes_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cga.secrets, "token_urlsafe", lambda _n: "FIXEDSTATE")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: "https://example.invalid/callback?code=CODE123&state=FIXEDSTATE",
    )

    with respx.mock:
        respx.post("https://api.github.com/app-manifests/CODE123/conversions").mock(
            return_value=httpx.Response(
                201, json={"id": 1, "pem": SENTINEL_PEM, "webhook_secret": SENTINEL_SECRET}
            )
        )
        result = cga.main(["--manual", "--env-path", str(tmp_path / ".env")])

    # main() doesn't accept a tmp_dir for --manual's scratch HTML file (only
    # _run_manual_flow's test seam does), so this one real-system-temp-dir
    # file is the unavoidable cost of exercising main()'s actual wiring --
    # clean it up rather than leaving it behind.
    written_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("wrote ")
    )
    Path(written_line.removeprefix("wrote ")).unlink(missing_ok=True)

    assert result == 0
    written = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "GITHUB_APP_ID=1" in written


def test_manifest_json_is_html_escaped_not_just_quote_escaped():
    """The old `.replace("'", "&#39;")` only handled single quotes; an `&` or
    `<` in an operator-supplied --name could still corrupt the HTML attribute
    or break the page."""
    manifest = cga.build_manifest("A & B <img onerror=alert(1)>", "https://e.com", "http://l/c")
    raw = json.dumps(manifest)
    escaped = html.escape(raw, quote=True)

    cga._CallbackHandler.manifest_json = escaped
    cga._CallbackHandler.state = "st"
    handler = cga._CallbackHandler.__new__(cga._CallbackHandler)
    form = handler._form()

    assert escaped in form
    assert "<img" not in form
    assert html.unescape(escaped) == raw
