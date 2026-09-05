"""Tests for GET /login — the static HTML login page shell."""
from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from main import app


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_login_page_serves_html_with_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body


async def test_login_page_has_username_password_and_remember_me_fields():
    client = await _client()
    body = (await client.get("/login")).text
    assert 'id="usernameInput"' in body
    assert 'id="passwordInput"' in body
    assert 'id="rememberInput"' in body
    assert 'type="checkbox"' in body


async def test_login_page_posts_json_to_api_login():
    """Pinned via regex rather than an exact literal chunk of source, so a
    harmless reformatting of the fetch() call (spacing, quote style, argument
    order) doesn't fail this test without an actual behavior change -- it
    still requires a real fetch(...) call targeting /api/login with a POST
    method, not just each substring appearing anywhere in the page."""
    client = await _client()
    body = (await client.get("/login")).text
    assert re.search(r'fetch\(\s*["\']/api/login["\']', body)
    assert re.search(r'method\s*:\s*["\']POST["\']', body)
