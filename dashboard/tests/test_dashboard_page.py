"""Tests for GET / — the static HTML dashboard page shell."""
from __future__ import annotations

import re

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


async def test_dashboard_page_language_options_are_plain_text_no_icon():
    """Same as the login page's language menu -- see its own test for the
    full rationale (a per-language icon was tried and reverted; both
    projects settled on plain text, no icon)."""
    client = await _client()
    body = (await client.get("/")).text
    lang_section = body[
        body.index('id="langPopupBackdrop"') : body.index('id="langPopupBackdrop"') + 600
    ]
    assert '<label><input type="radio" name="lang" value="en"> English</label>' in lang_section
    assert '<label><input type="radio" name="lang" value="he"> עברית</label>' in lang_section


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


async def test_guided_modal_replaces_its_derived_block_instead_of_appending():
    """Everything a successful Validate derives (model <select>, VERTEX_GCP_PROJECT
    keep/clear radios, installation-id label) must land in its own
    #guidedModalExtra container whose innerHTML is REPLACED per click.

    It used to be appended into #guidedModalBody with
    insertAdjacentHTML("beforeend"), and nothing removed the previous
    click's copy -- so a second Validate produced two
    id="guidedModelSelect" nodes and two projectChoice radio pairs.
    getElementById then read the stale FIRST select, so the model the
    operator picked in the visible one was silently discarded on Apply
    (confirmed live with Playwright: picked gemini-pro-latest, the request
    body carried the first select's value instead)."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="guidedModalExtra"' in body
    assert 'document.getElementById("guidedModalExtra").innerHTML = extra;' in body
    assert 'insertAdjacentHTML("beforeend", extra)' not in body
    assert "function resetGuidedValidation" in body


async def test_guided_modal_never_applies_an_empty_model():
    """A credential that validates OK but reports no models leaves the model
    <select> with zero options, so `.value` is "" -- Apply then posted
    model:"" and Render rejects an empty env-var value, so only the
    credential var landed and VERTEX_MODEL silently failed (reproduced live:
    `applied: VERTEX_GCP_SERVICE_ACCOUNT_KEY_1; failed: VERTEX_MODEL`). Validate
    must refuse to enable Apply in that state."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'if (family !== "github_app" && (result.models || []).length === 0)' in body
    assert 't("env_guided_no_models")' in body


async def test_guided_apply_reports_its_outcome_outside_the_dialog():
    """The applied/failed line used to be written into #guidedModalResult,
    which lives INSIDE <dialog id="guidedModal"> -- close() on the next line
    hid it immediately, so a partial apply was never visible and the
    refreshed var table (one added row) was the operator's only feedback.
    It must report into #renderSaveResult, which sits outside the dialog."""
    client = await _client()
    body = (await client.get("/")).text
    apply_start = body.index('document.getElementById("guidedApplyBtn").addEventListener')
    handler = body[apply_start : body.index('document.getElementById("guidedCancelBtn")')]
    assert 'document.getElementById("renderSaveResult").innerHTML' in handler
    assert 'document.getElementById("guidedModalResult").textContent' not in handler


async def test_guided_modal_resets_its_family_select_on_any_close():
    """Escape-dismissing a <dialog> does not run the Cancel button's
    handler, so resetting guidedSetupSelect there left it stuck on the
    chosen family -- and re-picking that same option fires no `change`
    event, making guided setup unreopenable for it. The reset belongs on the
    dialog's own `close` event, which covers Cancel, Apply and Escape."""
    client = await _client()
    body = (await client.get("/")).text
    close_listener = 'document.getElementById("guidedModal").addEventListener("close"'
    assert close_listener in body
    cancel_start = body.index('document.getElementById("guidedCancelBtn").addEventListener')
    cancel = body[cancel_start : body.index(close_listener)]
    assert 'guidedSetupSelect").value = ""' not in cancel


async def test_guided_modal_invalidates_validation_when_the_credential_changes():
    """Swapping the uploaded file / API key / App ID after a successful
    Validate used to leave guidedValidatedPayload (and the enabled Apply
    button) pointing at the PREVIOUSLY validated credential -- confirmed
    live: file B was in the picker, file A's bytes were written to Render.
    The key-slot select must stay exempt so changing it doesn't force
    another live provider call."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'document.getElementById("guidedModalBody").addEventListener("input"' in body
    assert 'if (event.target.id === "guidedSlotSelect") return;' in body


async def test_guided_validate_guards_missing_inputs_before_calling_out():
    """files[0] is undefined with nothing picked and FormData appends the
    string "undefined" -- the backend 422s with no `error` field, which
    rendered as a bare "✗ invalid: undefined". Guard locally instead, which
    also avoids a pointless live provider call."""
    client = await _client()
    body = (await client.get("/")).text
    assert 't("env_guided_missing_input")' in body
    assert 'formData.append("credential_file", document.getElementById' not in body


async def test_guided_validate_and_apply_disable_themselves_while_in_flight():
    """Neither button was disabled while its own fetch was in flight, so a
    fast double-click on Validate could fire two live provider-validation
    calls, and a fast double-click on Apply could push the same
    credential/model twice and trigger two Render deploys. Both handlers
    must disable themselves for the whole request and only re-enable Apply
    when guidedValidatedPayload is still set (mirroring
    resetGuidedValidation's own invariant), not unconditionally."""
    client = await _client()
    body = (await client.get("/")).text
    validate_start = body.index('document.getElementById("guidedValidateBtn").addEventListener')
    apply_start = body.index('document.getElementById("guidedApplyBtn").addEventListener')
    validate_handler = body[validate_start:apply_start]
    apply_handler = body[apply_start : body.index('document.getElementById("guidedCancelBtn")')]

    assert "validateBtn.disabled = true;" in validate_handler
    assert "applyBtn.disabled = !guidedValidatedPayload;" in validate_handler

    assert "validateBtn.disabled = true;" in apply_handler
    assert "applyBtn.disabled = true;" in apply_handler
    assert "applyBtn.disabled = !guidedValidatedPayload;" in apply_handler


async def test_guided_setup_strings_exist_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in ("env_guided_no_models", "env_guided_missing_input"):
        assert body.count(f"{key}:") == 2, key


async def test_config_panel_fields_are_declared_by_the_registry_not_by_markup():
    """The 17 config fields are generated from CONFIG_FIELDS at runtime.

    Replaces the old assertion that 17 literal id="cfg..." strings appear in
    the served HTML. Those ids are now produced by renderConfigForm() in the
    browser, so the served document no longer contains them -- the guarantee
    moves to the registry, which tests/test_config_field_registry.py checks
    for completeness against the runtime_config columns.
    """
    client = await _client()
    body = (await client.get("/")).text
    assert "CONFIG_FIELDS_BEGIN" in body
    assert "renderConfigForm" in body
    # The generic per-field markup is gone -- one shared info icon builder
    # replaces the 17 verbatim copies. Count the button template itself, not
    # the bare `data-action="info"` substring: that also appears inside the
    # two querySelectorAll selectors that wire the icons up.
    assert "infoIconHtml" in body
    assert body.count('class="info-icon" data-action="info"') == 2, (
        "exactly two info-icon button templates should exist -- keyCellHtml()'s "
        "render-vars one and infoIconHtml() -- config rows must build theirs "
        "from infoIconHtml(), not repeat the block per field"
    )


async def test_dashboard_page_declares_slot_config_rows_container():
    """The editable per-slot model/project/location grid must actually be
    present in the served page for its JS to render into."""
    client = await _client()
    resp = await client.get("/")
    assert 'id="slotConfigRows"' in resp.text


async def test_config_form_omits_blank_tuning_knob_fields_from_the_patch_body():
    """A blank tuning-knob input must be OMITTED from the PATCH body, not
    sent as null: these have no fallback, and a NULL column stops the
    dispatcher dead (review_queue/dispatcher_tuning_config.py).

    Since the registry refactor the rule lives in two collaborating places --
    readConfigValue() returns `undefined` for a blank tuning knob, and
    saveConfig() drops any key whose value is `undefined`. Both halves are
    pinned here; the pre-refactor `if (raw !== "")` phrasing pinned only the
    first, in a loop that no longer exists.
    """
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "return TUNING_KNOB_FIELDS.includes(field.key) ? undefined : null;" in body
    assert "if (value !== undefined) body[field.key] = value;" in body
    assert 'raw === "" ? null' not in body


async def test_slot_config_rows_are_editable():
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "slot-config-save" in body
    assert "/api/environment/slot-config" in body


async def test_config_controls_are_typed_not_generic_number_boxes():
    client = await _client()
    body = (await client.get("/")).text
    # duration fields carry a unit suffix and a humanised readout
    assert "function dur(" in body
    assert "function durLong(" in body
    # counts render a - n + stepper, not a bare number input
    assert "cfg-stepper" in body
    # the magnitude field offers presets and an explicit off state
    assert "cfg-presets" in body
    assert "cap_off" in body
    # the wallclock field enforces its format client-side
    assert "([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?" in body


async def test_every_programmatic_value_write_goes_through_sync_field():
    """Assigning .value fires no input event, so a direct assignment leaves a
    stale readout and a stale valid/invalid verdict. Four paths write values
    programmatically (populate, reset-to-default, preset chips, stepper); all
    four must route through syncField."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function syncField(" in body
    config_js = body.split("CONFIG_FIELDS_BEGIN")[1]
    assert ".value = " not in config_js.split("function syncField(")[0], (
        "a value is assigned outside syncField"
    )


async def test_config_form_groups_fields_and_validates_across_them():
    client = await _client()
    body = (await client.get("/")).text
    assert "GROUP_ORDER" in body
    assert "function validateGroup(" in body
    # both cross-field rules the server enforces are expressed client-side
    assert "cfg_err_base_exceeds_max" in body
    assert "cfg_err_backoff_base_exceeds_max" in body
    # the dead guard is finally fed
    assert "invalidConfigFields.add(" in body
    assert "invalidConfigFields.delete(" in body


async def test_cross_field_validation_is_keyed_by_group_not_by_field():
    """A rule must not report as passing because the field that would have
    failed it was not the one touched."""
    client = await _client()
    body = (await client.get("/")).text
    assert "invalidConfigFields.add(\"group:\"" in body


async def test_every_config_string_key_exists_in_both_languages():
    """Every t("cfg_...") / data-i18n="cfg_..." the config form references must
    resolve in en AND he -- a missing Hebrew key renders as the raw key."""
    client = await _client()
    body = (await client.get("/")).text
    referenced = set(re.findall(r'(?:data-i18n="|t\(")(cfg_[a-z0-9_]+)', body))
    # A dynamic key like data-i18n="cfg_group_${group}" matches only up to
    # the "$", leaving a truncated "cfg_group_" that is never a real static
    # key (the runtime concatenation always adds a suffix) -- drop these
    # rather than require a nonsensical literal "cfg_group_" entry.
    referenced = {k for k in referenced if not k.endswith("_")}
    assert referenced, "no cfg_ keys found -- the regex or the markup changed"
    for lang in ("en", "he"):
        block = body.split(f"      {lang}: {{", 1)[1].split("\n      },", 1)[0]
        missing = sorted(k for k in referenced if f"{k}:" not in block)
        assert not missing, f"{lang} is missing: {missing}"


async def test_config_number_inputs_suppress_the_native_spinner():
    """One stepper idiom in the form, not two.

    `count` fields carry our one-bit - n + stepper. Every other numeric field
    is a plain box. Left alone, type=number ALSO draws the browser's own
    spinner -- rounded, system-coloured, unreachable by our CSS -- so 11
    fields showed a second stepper idiom and 3 showed ours, which reads as
    arbitrary. DESIGN.md rejects platform chrome, and the design rejected
    these very spinners for counts (section 2.1); the same reasoning applies
    form-wide.
    """
    client = await _client()
    body = (await client.get("/")).text
    assert "::-webkit-inner-spin-button" in body
    assert "::-webkit-outer-spin-button" in body
    assert "appearance: textfield" in body


async def test_slot_config_save_result_renders_as_html_not_literal_markup():
    """formatSaveResult builds <div>/<ul>/<li>, so assigning it to textContent
    prints the markup verbatim in the panel. The three other call sites use
    innerHTML; saveSlotConfig did not (pre-dates this work -- commit 2e328ef).
    Safe because formatSaveResult esc()s every interpolated value."""
    client = await _client()
    body = (await client.get("/")).text
    assert "hint.textContent = formatSaveResult(" not in body
    assert "hint.innerHTML = formatSaveResult(" in body


async def test_no_label_repeats_a_unit_its_control_already_shows():
    """Duration controls render a "seconds" suffix beside the input, so a
    label ending in "(s)" / "(שנ')" states the unit twice."""
    client = await _client()
    body = (await client.get("/")).text
    for lang, unit in (("en", "(s)"), ("he", "(שנ')")):
        block = body.split(f"      {lang}: {{", 1)[1].split("\n      },", 1)[0]
        offenders = [
            line.strip()
            for line in block.splitlines()
            if line.strip().startswith("env_config_") and unit in line
        ]
        assert not offenders, f"{lang} label repeats the unit: {offenders}"


async def test_boolean_default_hint_reads_off_not_the_raw_wire_value():
    """`default false` leaks the JSON wire value into operator-facing copy."""
    client = await _client()
    body = (await client.get("/")).text
    assert "cfg_bool_off" in body
    assert "cfg_bool_on" in body
