"""Tests for dashboard/auth.py: credential check, session-token issue/verify, and
cookie helpers -- plus route-level (login/logout HTTP) behavior and
require_session's HTTP-gate behavior (the /healthz and /webhook routes stay
reachable with no cookie at all; every /api/* and page route is gated).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from config import settings
from main import app
from dashboard import auth


def _request_with_cookie(cookie_header: str | None) -> Request:
    headers = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode()))
    return Request({"type": "http", "headers": headers})


def test_verify_credentials_accepts_the_right_username_and_password():
    assert auth.verify_credentials("test-operator", "test-password") is True


def test_verify_credentials_rejects_wrong_username():
    assert auth.verify_credentials("wrong", "test-password") is False


def test_verify_credentials_rejects_wrong_password():
    assert auth.verify_credentials("test-operator", "wrong") is False


def test_verify_credentials_rejects_both_wrong():
    assert auth.verify_credentials("wrong", "wrong") is False


def test_create_session_token_defaults_to_a_12_hour_expiry():
    token = auth.create_session_token(remember=False)
    payload = jwt.decode(token, settings.dashboard_session_secret, algorithms=["HS256"])
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=11, minutes=59) < delta <= timedelta(hours=12)


def test_create_session_token_remember_extends_to_30_days():
    token = auth.create_session_token(remember=True)
    payload = jwt.decode(token, settings.dashboard_session_secret, algorithms=["HS256"])
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)


def test_set_session_cookie_sets_httponly_secure_samesite_strict():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=False)
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "secure" in cookie_header
    assert "samesite=strict" in cookie_header


def test_set_session_cookie_default_uses_the_12_hour_max_age():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=False)
    assert f"Max-Age={12 * 60 * 60}" in response.headers["set-cookie"]


def test_set_session_cookie_remember_uses_the_30_day_max_age():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=True)
    assert f"Max-Age={30 * 24 * 60 * 60}" in response.headers["set-cookie"]


def test_clear_session_cookie_expires_immediately():
    response = JSONResponse({})
    auth.clear_session_cookie(response)
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_require_session_accepts_a_freshly_issued_token():
    token = auth.create_session_token(remember=False)
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}")
    await auth.require_session(request)  # must not raise


async def test_require_session_rejects_a_missing_cookie():
    request = _request_with_cookie(None)
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_a_tampered_token():
    token = auth.create_session_token(remember=False)
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}x")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_an_expired_token():
    expired = jwt.encode(
        {"exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        settings.dashboard_session_secret,
        algorithm="HS256",
    )
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={expired}")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_a_token_signed_with_a_different_secret():
    token = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "a-completely-different-secret-value-for-testing-only",
        algorithm="HS256",
    )
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def _no_login_delay(monkeypatch):
    """Requested by name from route tests that hit /api/login, so the fixed
    delay doesn't slow the suite down. Deliberately not autouse: the tests
    above this point never touch the route layer or this function at all."""
    async def _noop() -> None:
        return None

    monkeypatch.setattr(auth, "_delay_after_login_failure", _noop)


async def test_login_with_correct_credentials_sets_a_session_cookie(_no_login_delay):
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "test-password", "remember": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert auth.SESSION_COOKIE_NAME in resp.cookies


async def test_login_with_wrong_password_returns_the_generic_reason_and_no_cookie(_no_login_delay):
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "wrong", "remember": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "invalid_credentials"}
    assert auth.SESSION_COOKIE_NAME not in resp.cookies


async def test_login_with_wrong_username_returns_the_identical_generic_reason(_no_login_delay):
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "wrong", "password": "test-password", "remember": False},
    )
    assert resp.json() == {"valid": False, "reason": "invalid_credentials"}


async def test_login_with_non_ascii_password_returns_invalid_credentials_not_500(_no_login_delay):
    """hmac.compare_digest raises TypeError on non-ASCII str input -- before
    the fix, this would 500 instead of degrading to a normal wrong-password
    response. A hostile (or simply non-English-keyboard) visitor submitting a
    non-ASCII password must get the same generic rejection as any other wrong
    password, never a crash."""
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "пароль", "remember": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "invalid_credentials"}


async def test_login_succeeds_when_the_configured_credential_itself_is_non_ascii(
    monkeypatch, _no_login_delay,
):
    """A Hebrew or accented DASHBOARD_USERNAME/DASHBOARD_PASSWORD is a
    realistic operator choice, given this app's own Hebrew-language login
    page -- the comparison must not merely avoid crashing on a non-ASCII
    *attempt*, it must actually accept a non-ASCII *configured* credential
    submitted correctly."""
    monkeypatch.setattr(settings, "dashboard_username", "מפעיל")
    monkeypatch.setattr(settings, "dashboard_password", "סיסמה")
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "מפעיל", "password": "סיסמה", "remember": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


async def test_login_failure_triggers_the_fixed_delay(monkeypatch):
    calls = []

    async def _record() -> None:
        calls.append(1)

    monkeypatch.setattr(auth, "_delay_after_login_failure", _record)
    client = await _client()
    await client.post(
        "/api/login", json={"username": "wrong", "password": "wrong", "remember": False}
    )
    assert calls == [1]


async def test_login_remember_true_sets_the_30_day_max_age(_no_login_delay):
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "test-password", "remember": True},
    )
    assert f"Max-Age={30 * 24 * 60 * 60}" in resp.headers["set-cookie"]


async def test_logout_clears_the_session_cookie():
    client = await _client()
    resp = await client.post("/api/logout")
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert "Max-Age=0" in resp.headers["set-cookie"]


async def test_unauthenticated_api_dashboard_request_gets_401_json():
    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 401
    assert resp.json() == {"valid": False, "reason": "unauthenticated"}


async def test_unauthenticated_dashboard_page_request_redirects_to_login():
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)
    resp = await client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_healthz_is_reachable_with_no_cookie_at_all():
    """Spec section 6: /healthz must stay reachable unauthenticated -- it is
    outside dashboard_router (no require_session dependency) and this pins
    that directly, rather than relying on it being true only incidentally
    (every existing /healthz test happens to never attach a cookie)."""
    client = await _client()
    resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_webhook_401_with_no_cookie_is_its_own_hmac_failure_not_the_auth_gate():
    """/webhook must also stay reachable with no cookie. It legitimately 401s
    on a missing/bad HMAC signature -- that's expected -- but the point of
    this test is confirming that 401 is webhook's own, not a future refactor
    (e.g. to global auth middleware) accidentally routing it through the
    dashboard's SessionRequired gate instead."""
    client = await _client()
    resp = await client.post("/webhook", content=b'{"action": "opened"}')
    assert resp.status_code == 401
    # webhook's own 401 is a bare Response with no body (unlike the auth
    # gate's JSONResponse) -- resp.json() would raise on the empty body, so
    # parse defensively rather than assume either shape.
    try:
        body = resp.json()
    except ValueError:
        body = None
    assert body != {"valid": False, "reason": "unauthenticated"}
