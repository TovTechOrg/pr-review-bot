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


async def test_login_page_password_toggle_uses_the_hidden_attribute_not_the_idl_property():
    """Real bug, caught while wiring the crossed-eye icon: setting the
    `.hidden` IDL property on an <svg> doesn't reliably reflect to the
    `hidden` *attribute* in every browser (unlike HTMLElement), which
    silently no-ops the CSS `svg[hidden] { display: none }` rule -- both eye
    icons stack on top of each other and the icon never visually swaps,
    regardless of which state maps to which icon. toggleAttribute operates
    on the actual attribute and must not regress back to `.hidden = ...`."""
    client = await _client()
    body = (await client.get("/login")).text
    assert '.hidden =' not in body
    assert 'getElementById("passwordEyeOpen").toggleAttribute("hidden", visible)' in body
    assert 'getElementById("passwordEyeClosed").toggleAttribute("hidden", !visible)' in body


async def test_login_page_self_hosts_its_display_font_and_has_no_emoji_icons():
    """Same contract as the dashboard page: the pixel display face must be
    self-hosted (never a Google Fonts CDN link an outage could silently
    revert), and the theme/language toggle must use authored SVG icons, not
    emoji."""
    client = await _client()
    body = (await client.get("/login")).text
    assert "/static/fonts/press-start-2p" in body
    assert "fonts.googleapis.com" not in body
    assert "fonts.gstatic.com" not in body
    for emoji in ("🖥️", "☀️", "🌙", "🇺🇸", "🇮🇱"):
        assert emoji not in body


async def test_login_page_language_options_carry_authored_flag_icons():
    """The language radio list was missing any per-language icon at all
    (unlike the onboarding wizard's own language menu, which uses flag
    emoji) -- synced here with an authored SVG flag icon per option,
    following this system's stroke convention (viewBox 0 0 24 24,
    stroke="currentColor", stroke-width="2", round caps/joins), not emoji."""
    client = await _client()
    body = (await client.get("/login")).text
    lang_section = body[
        body.index('id="langPopupBackdrop"') : body.index('id="langPopupBackdrop"') + 1200
    ]
    en_label = lang_section[
        lang_section.index('value="en"') : lang_section.index('value="he"')
    ]
    he_label = lang_section[lang_section.index('value="he"') :]
    for label in (en_label, he_label):
        assert '<svg viewBox="0 0 24 24"' in label
        assert 'stroke="currentColor"' in label
        assert 'stroke-width="2"' in label
    # The two icons must actually differ from each other (stripes vs. a
    # Star-of-David emblem) -- not the same glyph copy-pasted twice, which
    # would defeat the point of a per-language icon.
    assert en_label != he_label
