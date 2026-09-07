"""Tests for GET / — the static HTML dashboard page shell."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from main import app
from dashboard import auth


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    )


async def test_dashboard_page_serves_html_with_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body
    assert "עברית" in body
    assert 'dir="ltr"' in body


async def test_dashboard_no_longer_served_at_slash_dashboard():
    """The page moved from /dashboard to / (no redirect, no duplicate route)
    — /dashboard should be gone, not just an alias."""
    client = await _client()
    resp = await client.get("/dashboard")
    assert resp.status_code == 404


async def test_dashboard_page_includes_polling_and_rendering_hooks():
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "/api/dashboard" in body
    assert "setInterval" in body
    assert "renderReviews" in body
    assert "renderStats" in body


async def test_render_stats_guards_on_queue_by_status_error_not_bare_queue_error():
    """renderStats's degrade-guard must check queue.by_status.error.

    /api/dashboard's queue payload is always {"by_status": ..., "backoff": ...} —
    there is no top-level "error" key on queue itself. build_dashboard_payload()
    (dashboard/router.py) degrades queue.by_status to {"error": "data unavailable"}
    on a store failure, not queue as a whole. A guard written as `queue.error`
    is permanently undefined and never trips, so a degraded queue would render
    a garbled stat tile (e.g. "q_error: data unavailable") instead of clearing
    the stats section. The guard must dereference queue.by_status?.error.
    """
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "queue.by_status?.error" in body
    assert "queue.error" not in body


async def test_dashboard_page_guards_null_est_cost_usd_before_tofixed():
    """est_cost_usd is nullable (Task 3 made unpriced reviews serialize as
    JSON null). Calling .toFixed(4) directly on it throws inside
    renderReviews, which is called from refreshDashboard's try block -- so
    one unpriced review among the most-recent 50 blanks the entire reviews
    table on every poll. The renderer must null-guard before .toFixed,
    following the same `?? "?"` idiom used for finding.line."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'review.est_cost_usd === null ? "—" : `$${review.est_cost_usd.toFixed(4)}`' in body


async def test_dashboard_page_escapes_llm_text_before_innerhtml():
    """finding.*/specialist.error are attacker-influenced LLM text; the page
    must run them through esc() before interpolating into innerHTML, not
    just LLM-controlled-looking closed enums like severity/status."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "function esc(value)" in body
    assert "esc(specialist.error" in body
    assert "esc(text)" in body


async def test_dashboard_page_has_translated_specialist_names():
    """Specialist display names must come from STRINGS, not the raw literal
    'Security'/'Performance'/'Code Quality' field, so Hebrew mode doesn't
    half-translate ('Security: תקין')."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "sp_name_security" in body
    assert "אבטחה" in body
    assert "SPECIALIST_KEY" in body


async def test_dashboard_page_renders_queue_and_backoff_as_chips_not_one_string():
    """The Queue and Provider-backoff stat tiles must render each status/
    provider as its own chip element, not one run-on string joined with
    ' · ' — a single string wraps unpredictably at narrow widths (e.g. two
    unrelated statuses landing on the same visual line), which is exactly
    the mobile bug this pins against a regression."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "tile-chip-list" in body
    assert '<span class="tile-chip">' in body
    assert "queueChips" in body
    assert "backoffChips" in body
    # the old bug joined every status/provider into one run-on string with
    # this separator; the chip-based rendering must not reintroduce it
    assert '.join(", ")' not in body


async def test_stored_lang_and_theme_are_parsed_defensively():
    """An unrecognized stored value (not "en"/"he", not "light"/"dark"/
    "system") must not throw inside applyLanguage (STRINGS[currentLang][key]
    would throw for an unknown currentLang), which would abort
    DOMContentLoaded before any event listener attaches."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function readStoredLang" in body
    assert "function readStoredTheme" in body
    assert "KNOWN_LANGS.includes(stored)" in body
    assert "KNOWN_THEMES.includes(stored)" in body
    assert 'localStorage.getItem("dashboard_lang") || "en"' not in body
    assert 'localStorage.getItem("dashboard_theme") || "system"' not in body


async def test_dashboard_page_has_a_logout_control_that_posts_to_api_logout():
    """The dashboard must expose a reachable way to log out -- POST /api/logout
    exists and is tested at the API level, but was unreachable from any page
    before this: no button anywhere called it."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'id="logoutBtn"' in body
    assert '"/api/logout"' in body
    assert 'method: "POST"' in body


async def test_dashboard_page_refresh_redirects_to_login_on_401():
    """An expired/invalid session must not render as a permanent generic
    error banner -- refreshDashboard must check response.status for 401 and
    redirect to /login, rather than trying (and failing) to parse the body
    as the normal payload shape."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "response.status === 401" in body
    assert 'window.location.href = "/login"' in body


async def test_dashboard_page_anchors_popups_to_their_button():
    """Popups must be positioned near the button that opened them (an
    absolutely-positioned popup placed via getBoundingClientRect), not
    centered on the screen regardless of which button was clicked."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "function positionPopup" in body
    assert "getBoundingClientRect" in body
    assert "openPopup(\"themePopupBackdrop\", event.currentTarget)" in body
    assert "openPopup(\"langPopupBackdrop\", event.currentTarget)" in body


async def test_dashboard_page_has_one_window_per_specialist():
    """Direction contract FIRST VIEWPORT (.impeccable/surfaces/dashboard.md):
    Security/Performance/Code Quality each get their own titled window, not
    fields inside one shared card -- a regression here silently collapses
    the redesign's central composition back toward the retired stat-tile
    grid without any test catching it."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "function renderSpecialists" in body
    assert '["Security", "Performance", "Code Quality"]' in body
    for name in ("Security", "Performance", "Code Quality"):
        assert f'data-spec-body="{name}"' in body
        assert f'data-spec-dot="{name}"' in body
    # Called from the same refresh path as the other real-data renderers,
    # not left dead/unreferenced.
    assert "renderSpecialists(data.reviews)" in body


async def test_dashboard_page_specialist_window_shows_empty_state_with_no_reviews():
    """renderSpecialists must handle an empty reviews list without throwing
    (reviews[0] is undefined) and render the same empty-state string the
    Activity.log window already uses, rather than a blank or broken window
    on a freshly-deployed instance with no recorded reviews yet."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "latest?.specialists?.find" in body
    assert 't("empty_reviews")' in body


async def test_dashboard_page_retired_the_stat_tile_hero_metric_grid():
    """The old stats-grid/stat-tile pattern (craft-floor's banned
    hero-metric template: big number, small label, nested in a card) was
    deliberately replaced by plain kv-row/kv-label/kv-value facts -- pin
    both the presence of the replacement and the absence of the retired
    pattern, so neither regresses silently."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "kv-row" in body
    assert "kv-label" in body
    assert "kv-value" in body
    assert "stats-grid" not in body
    assert "stat-tile" not in body


async def test_dashboard_page_live_signals_share_the_same_running_state():
    """The Queue window's dot, the traceable-thread connector, and the
    Activity.log window's dot must all reflect the same real
    queue.by_status.running signal -- Activity.log's dot was hardcoded
    permanently lit in an earlier pass (a real finish-review finding); pin
    that all three are driven by one `running` value, not two wired and one
    left static."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'id="systemLiveDot"' in body
    assert 'id="reviewThread"' in body
    assert 'id="activityLiveDot"' in body
    # None of the three may carry a static "is-live" class in the served
    # markup -- all three must be toggled at runtime off real data.
    assert 'id="systemLiveDot" class="win-live is-live"' not in body
    assert 'id="reviewThread" class="thread is-live"' not in body
    assert 'id="activityLiveDot" class="win-live is-live"' not in body
    for dot_id in ("systemLiveDot", "reviewThread", "activityLiveDot"):
        assert f'getElementById("{dot_id}").classList.toggle("is-live", running)' in body


async def test_dashboard_page_self_hosts_its_display_font():
    """The pixel display face must be served from this app's own /static
    mount, never a third-party CDN -- an unreachable Google Fonts request
    would otherwise silently revert the entire display voice with no
    indication, on a page whose whole job is reading system health at a
    glance. Also pins that no emoji icon crept back in (the theme/language
    toggle was rebuilt on authored SVGs after a finish-review finding)."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "/static/fonts/press-start-2p" in body
    assert "fonts.googleapis.com" not in body
    assert "fonts.gstatic.com" not in body
    for emoji in ("🖥️", "☀️", "🌙", "🇺🇸", "🇮🇱"):
        assert emoji not in body


async def test_dashboard_page_language_options_carry_authored_flag_icons():
    """Same sync as the login page's language menu -- see its own test for
    the full rationale."""
    client = await _client()
    body = (await client.get("/")).text
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
    assert en_label != he_label


async def test_env_info_icon_has_no_native_title_tooltip():
    """The button used to carry both a native `title` attribute (the
    browser's own delayed hover tooltip) and the authored `.info-tooltip`
    span (shown via CSS on hover/focus/click) -- hovering an info icon
    showed both at once, overlapping. `aria-label` alone is enough for the
    accessible name; the authored tooltip is the only visible one now."""
    client = await _client()
    body = (await client.get("/")).text
    fn_start = body.index("function keyCellHtml")
    fn_body = body[fn_start : body.index("function maskedValue")]
    assert 'class="info-icon"' in fn_body
    assert "title=" not in fn_body
    assert 'aria-label="${esc(t("env_info_label"))}"' in fn_body
    assert 'class="info-tooltip"' in fn_body


async def test_static_fonts_are_served_publicly_without_a_session():
    """The login page (pre-authentication) also uses the self-hosted pixel
    font, so its mount must not sit behind require_session -- unlike every
    other dashboard/environment route."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/static/fonts/press-start-2p-v16-latin-regular.woff2")
    assert resp.status_code == 200
    assert resp.headers["content-type"] in ("font/woff2", "application/font-woff2")
