# Dashboard Typed Config Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `#configForm`'s 17 generic number/text inputs with typed
controls generated from one declarative registry, policed by a test that
probes the real server-side predicates.

**Architecture:** A JSON-parseable `CONFIG_FIELDS` registry in
`dashboard.html` becomes the single client-side description of every
`runtime_config`-backed field. It drives markup generation, control
selection, validation, populate and PATCH serialization. A new pytest
parses that registry out of the served HTML and probes each Python
predicate at its boundary, so a client attribute can never silently
disagree with the server rule it mirrors.

**Tech Stack:** Vanilla JS (no framework, no build step) inside a single
self-contained `dashboard/static/dashboard.html`; pytest + httpx ASGI
transport for server-side tests; Playwright for the visual review.

**Spec:** `docs/superpowers/specs/2026-09-11-dashboard-typed-config-controls-design.md`

## Global Constraints

- **Server-side validation is not touched.** `cooldown_config.problems()`,
  `usage_cap_config.problems()` and `dispatcher_tuning_config.problems()`
  remain the sole authority on validity. No task may weaken, bypass, or
  re-implement their verdict as the thing deciding whether a write happens.
- **No new runtime dependency and no build step.** `dashboard.html` stays a
  single self-contained file served by `dashboard/router.py`.
- **The omit-don't-null rule survives.** A blank tuning-knob field must be
  omitted from the PATCH body entirely, never sent as `null` — a NULL column
  stops the dispatcher dead. See `dashboard.html:2632-2639`.
- **The registry block must stay JSON-parseable**: double-quoted keys and
  string values, no comments inside the array, no trailing commas, no JS
  expressions. `tests/test_config_field_registry.py` reads it with
  `json.loads`.
- **API key vs. column name.** For 15 of 17 fields these are equal. The two
  exceptions are `usage_cap_tokens` → column `key_usage_token_cap` and
  `usage_cap_reset` → column `key_usage_reset_time_utc`. Every registry
  entry carries both `key` (API/PATCH) and `column` (`runtime_config`).
- **`step="any"`** on every float field; integer fields use `step="1"`.
- **Before pushing:** `uv run pytest -v` and `uv run ruff check .` must both
  be green (root `CLAUDE.md`). Before any push to `main`, invoke
  `deploy-verify`. Before calling the work done, invoke `ui-visual-review`.
- **Parked Minor findings** from any review must be logged in `ISSUES.md`'s
  Parked Issues section before the branch is considered done.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `dashboard/static/dashboard.html` | The registry, generated markup, control kinds, validation, previews, i18n strings | Modify |
| `tests/test_config_field_registry.py` | Probes registry bounds/defaults against the real Python predicates | Create |
| `dashboard/tests/test_dashboard_page.py` | Existing page-shape assertions; two tests change | Modify |
| `ISSUES.md` | Parked findings | Modify (Task 7) |

---

### Task 1: The registry and its conformance test

Introduces `CONFIG_FIELDS` **alongside** the existing hand-written markup
(which Task 2 removes) and the test that keeps it honest. This task fixes
the live `min="0"` bug on `dispatcher_idle_sleep_seconds`.

**Files:**
- Modify: `dashboard/static/dashboard.html` (insert registry near `:2563`, replacing `TUNING_KNOB_FIELDS`/`INTEGER_KNOB_FIELDS`; fix the attribute at `:813`)
- Create: `tests/test_config_field_registry.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `CONFIG_FIELDS` — a JS array of 17 objects, each with at least
  `key: string` and `column: string`, delimited by the exact marker comments
  `// CONFIG_FIELDS_BEGIN` and `// CONFIG_FIELDS_END`. Tasks 2–6 all read it.
  Also `tests/test_config_field_registry.py::load_registry()` returning
  `list[dict]`, reused by Task 5's tests.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_field_registry.py`:

```python
"""The dashboard's CONFIG_FIELDS registry must agree with the server-side
predicates it mirrors.

The registry is a client-side duplicate of bounds that live in Python. This
file is the mechanism that stops the duplicate drifting -- see
docs/superpowers/specs/2026-09-11-dashboard-typed-config-controls-design.md
section 7.1. It probes each predicate at its boundary rather than reading a
number out of it, because the predicates are opaque lambdas from which no
bound can be extracted mechanically.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from review_queue import (
    cooldown_config,
    dispatcher_tuning_config,
    runtime_config_defaults,
    store,
)

DASHBOARD_HTML = (
    Path(__file__).resolve().parents[1] / "dashboard" / "static" / "dashboard.html"
)

_REGISTRY_RE = re.compile(
    r"// CONFIG_FIELDS_BEGIN.*?const CONFIG_FIELDS = (\[.*?\]);\s*// CONFIG_FIELDS_END",
    re.S,
)

# Every field the config panel edits, by API key. Pinned here so adding a
# field to the form without adding it to the registry fails loudly.
EXPECTED_KEYS = {
    "provider",
    "cooldown_base_seconds",
    "cooldown_factor",
    "cooldown_max_seconds",
    "dispatcher_failure_base_backoff_seconds",
    "dispatcher_failure_max_backoff_seconds",
    "dispatcher_backoff_jitter_seconds",
    "dispatcher_max_failure_attempts",
    "usage_cap_tokens",
    "usage_cap_reset",
    "llm_request_timeout_seconds",
    "dispatcher_default_retry_after_seconds",
    "dispatcher_min_retry_after_seconds",
    "dispatcher_idle_sleep_seconds",
    "dispatcher_notice_sweep_batch_size",
    "dispatcher_max_notice_post_attempts",
    "review_draft_prs",
}

EPS = 1e-9


def load_registry() -> list[dict]:
    """CONFIG_FIELDS, parsed out of the served HTML."""
    match = _REGISTRY_RE.search(DASHBOARD_HTML.read_text(encoding="utf-8"))
    assert match, "CONFIG_FIELDS block not found between its marker comments"
    return json.loads(match.group(1))


def _predicates() -> dict:
    """Column name -> the real server-side predicate for that column."""
    found = {}
    for key, predicate, _description in cooldown_config._BOUNDS:
        found[key] = predicate
    for key, predicate, _description in dispatcher_tuning_config._BOUNDS:
        found[key] = predicate
    # dispatcher_idle_sleep_seconds has NO _BOUNDS entry -- it is read through
    # a separate throttled path, so its rule lives as inline code in
    # dashboard/environment.py:868-875. Transcribed here deliberately; if it
    # ever moves into dispatcher_tuning_config._BOUNDS, delete this line and
    # the loop above will pick it up.
    found["dispatcher_idle_sleep_seconds"] = lambda v: v > 0
    # key_usage_token_cap likewise: usage_cap_config.problems() hand-writes it
    # rather than using a _BOUNDS table, because a None cap is VALID ("cap
    # intentionally disabled"). Only a non-positive number is rejected.
    found["key_usage_token_cap"] = lambda v: v > 0
    return found


def test_registry_is_json_parseable_and_covers_every_field():
    fields = load_registry()
    assert {f["key"] for f in fields} == EXPECTED_KEYS
    assert len(fields) == len(EXPECTED_KEYS), "duplicate key in CONFIG_FIELDS"


def test_every_registry_column_exists_in_the_table():
    declared = {name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS}
    for field in load_registry():
        assert field["column"] in declared, (
            f"{field['key']} names column {field['column']}, "
            "which runtime_config does not have"
        )


@pytest.mark.parametrize(
    "field", [f for f in load_registry() if "min" in f], ids=lambda f: f["key"]
)
def test_registry_min_matches_the_server_predicate(field):
    """Probe the predicate either side of the declared bound.

    An inclusive bound must ACCEPT itself and reject just below it; an
    exclusive bound must REJECT itself and accept just above it.
    """
    predicate = _predicates()[field["column"]]
    minimum = field["min"]
    if field["exclusive"]:
        assert not predicate(minimum), (
            f"{field['key']}: registry says min {minimum} is excluded, "
            "but the server accepts it"
        )
        assert predicate(minimum + EPS)
    else:
        assert predicate(minimum), (
            f"{field['key']}: registry offers min {minimum}, "
            "but the server rejects it"
        )
        assert not predicate(minimum - EPS)


def test_registry_defaults_match_the_declared_column_defaults():
    declared = runtime_config_defaults.declared_defaults()
    for field in load_registry():
        column = field["column"]
        if column in runtime_config_defaults.NO_DEFAULT_BY_DESIGN:
            assert field["default"] is None, (
                f"{field['key']} has no default by design; "
                "the registry must say null"
            )
            continue
        if column not in declared:
            # `provider` is provisioner-owned and has no declared default.
            assert field["default"] is None
            continue
        assert field["default"] == declared[column], (
            f"{field['key']}: registry default {field['default']!r} != "
            f"declared default {declared[column]!r}"
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_field_registry.py -v`
Expected: every test FAILS with `AssertionError: CONFIG_FIELDS block not
found between its marker comments` — the registry does not exist yet.

- [ ] **Step 3: Add the registry**

In `dashboard/static/dashboard.html`, **replace** the `TUNING_KNOB_FIELDS`
and `INTEGER_KNOB_FIELDS` declarations (`:2563-2587`) with the registry
below. Keep `tuningKnobFieldId` — later tasks still use it to derive DOM ids.

```js
    // CONFIG_FIELDS_BEGIN -- machine-read by tests/test_config_field_registry.py,
    // which probes each `min`/`exclusive` pair against the real Python
    // predicate. Keep this array JSON-parseable: double-quoted keys and string
    // values, no comments inside it, no trailing commas, no JS expressions.
    //
    // `key`    is the /api/environment/config payload and PATCH body name.
    // `column` is the runtime_config column. They differ for the usage-cap
    //          pair only; the test resolves predicates and defaults by column.
    const CONFIG_FIELDS = [
      {"key": "provider", "column": "provider", "kind": "enum", "group": "pinned", "default": null},

      {"key": "cooldown_base_seconds", "column": "cooldown_base_seconds", "kind": "duration", "group": "cooldown", "min": 0, "exclusive": false, "step": "any", "unit": "seconds", "default": 300.0},
      {"key": "cooldown_factor", "column": "cooldown_factor", "kind": "duration", "group": "cooldown", "min": 1, "exclusive": false, "step": 0.5, "unit": "each_escalation", "humanize": false, "default": 2.0},
      {"key": "cooldown_max_seconds", "column": "cooldown_max_seconds", "kind": "duration", "group": "cooldown", "min": 0, "exclusive": true, "step": "any", "unit": "seconds", "default": 3600.0},

      {"key": "dispatcher_failure_base_backoff_seconds", "column": "dispatcher_failure_base_backoff_seconds", "kind": "duration", "group": "backoff", "min": 0, "exclusive": false, "step": "any", "unit": "seconds", "default": 2.0},
      {"key": "dispatcher_failure_max_backoff_seconds", "column": "dispatcher_failure_max_backoff_seconds", "kind": "duration", "group": "backoff", "min": 0, "exclusive": true, "step": "any", "unit": "seconds", "default": 300.0},
      {"key": "dispatcher_backoff_jitter_seconds", "column": "dispatcher_backoff_jitter_seconds", "kind": "duration", "group": "backoff", "min": 0, "exclusive": false, "step": "any", "unit": "seconds", "default": 0.0},
      {"key": "dispatcher_max_failure_attempts", "column": "dispatcher_max_failure_attempts", "kind": "count", "group": "backoff", "min": 1, "exclusive": false, "integer": true, "unit": "attempts", "default": 5},

      {"key": "usage_cap_tokens", "column": "key_usage_token_cap", "kind": "magnitude", "group": "usage_cap", "min": 0, "exclusive": true, "integer": true, "nullable": "cap_off", "presets": [10000, 100000, 500000, 1000000, 5000000], "default": null},
      {"key": "usage_cap_reset", "column": "key_usage_reset_time_utc", "kind": "wallclock", "group": "usage_cap", "default": "04:00:00"},

      {"key": "llm_request_timeout_seconds", "column": "llm_request_timeout_seconds", "kind": "duration", "group": "limits", "min": 0, "exclusive": true, "step": "any", "unit": "seconds", "default": 45.0},
      {"key": "dispatcher_default_retry_after_seconds", "column": "dispatcher_default_retry_after_seconds", "kind": "duration", "group": "limits", "min": 0, "exclusive": false, "step": "any", "unit": "seconds", "default": 60.0},
      {"key": "dispatcher_min_retry_after_seconds", "column": "dispatcher_min_retry_after_seconds", "kind": "duration", "group": "limits", "min": 0, "exclusive": false, "step": "any", "unit": "seconds", "default": 1.0},
      {"key": "dispatcher_idle_sleep_seconds", "column": "dispatcher_idle_sleep_seconds", "kind": "duration", "group": "limits", "min": 0, "exclusive": true, "step": "any", "unit": "seconds", "default": 1.0},
      {"key": "dispatcher_notice_sweep_batch_size", "column": "dispatcher_notice_sweep_batch_size", "kind": "count", "group": "limits", "min": 1, "exclusive": false, "integer": true, "unit": "per_batch", "default": 20},
      {"key": "dispatcher_max_notice_post_attempts", "column": "dispatcher_max_notice_post_attempts", "kind": "count", "group": "limits", "min": 1, "exclusive": false, "integer": true, "unit": "attempts", "default": 3},

      {"key": "review_draft_prs", "column": "review_draft_prs", "kind": "bool", "group": "standalone", "default": false}
    ];
    // CONFIG_FIELDS_END

    // The 9 knobs whose PATCH keys must be OMITTED when blank rather than
    // sent as null (see saveConfig). Derived from the registry rather than
    // re-listed: everything in the two dispatcher-tuning groups plus the LLM
    // timeout, i.e. every field that has no fallback and whose NULL column
    // stops the dispatcher dead.
    const TUNING_KNOB_FIELDS = CONFIG_FIELDS
      .filter((f) => f.group === "backoff" || f.group === "limits")
      .map((f) => f.key);

    const INTEGER_KNOB_FIELDS = new Set(
      CONFIG_FIELDS.filter((f) => f.integer && f.kind === "count").map((f) => f.key)
    );

    function fieldByKey(key) {
      return CONFIG_FIELDS.find((f) => f.key === key);
    }
```

- [ ] **Step 4: Run the test to verify the idle-sleep bug is caught**

Run: `uv run pytest tests/test_config_field_registry.py -v`
Expected: coverage/column/default tests PASS. **All `min` probes PASS** —
the registry declares the *correct* bound
(`"min": 0, "exclusive": true`) for `dispatcher_idle_sleep_seconds`, while
the stale `min="0"` still sits in the hand-written markup at `:813`. The
markup is not yet registry-driven, so the test cannot see it; Step 5 fixes
the markup so the two agree before Task 2 makes the registry authoritative.

- [ ] **Step 5: Fix the stale markup attribute**

In `dashboard/static/dashboard.html:813`, change:

```html
          <input id="cfgDispatcherIdleSleepSeconds" type="number" step="any" min="0">
```

to:

```html
          <input id="cfgDispatcherIdleSleepSeconds" type="number" step="any" min="0.001">
```

This is a stopgap that Task 2 deletes along with the rest of the
hand-written markup; it exists so no commit in this branch leaves the form
offering a value the server refuses.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all PASS. `TUNING_KNOB_FIELDS` and `INTEGER_KNOB_FIELDS` are now
derived rather than literal, so
`test_config_form_omits_blank_tuning_knob_fields_from_the_patch_body`
(`dashboard/tests/test_dashboard_page.py:452`) proves the derivation is
equivalent.

- [ ] **Step 7: Commit**

```bash
git add dashboard/static/dashboard.html tests/test_config_field_registry.py
git commit -m "Add the config field registry and the test that keeps it honest

The registry duplicates bounds that live in Python predicates. The new test
probes each predicate at its boundary rather than reading a number out of it
-- the predicates are opaque lambdas, so a bound can only be confirmed by
evaluating either side of it.

Also corrects dispatcher_idle_sleep_seconds' min, which offered 0 while the
server rejects 0 outright as a value that would busy-loop the dispatcher."
```

---

### Task 2: Generate the form from the registry

A **pure refactor**: the rendered form must look and behave identically.
Generating first, then changing control shapes in Task 3, keeps the diff
that changes appearance separate from the diff that changes structure.

**Files:**
- Modify: `dashboard/static/dashboard.html` (delete `:694-935` hand-written rows; add `renderConfigForm`; rewrite `populateConfigForm` and `saveConfig`)
- Modify: `dashboard/tests/test_dashboard_page.py:419`

**Interfaces:**
- Consumes: `CONFIG_FIELDS`, `fieldByKey`, `tuningKnobFieldId` (Task 1).
- Produces:
  - `renderConfigForm()` — builds all 17 rows into `#configForm`; called once at load, before the first `fetchEnvironmentConfig()`.
  - `infoIconHtml(descKey)` → `string` — the shared info-icon + tooltip block, replacing 17 verbatim copies.
  - `configRowHtml(field)` → `string` — one `label │ control-line / error-line / hint-line` row.
  - `controlHtml(field)` → `string` — the inner control only; Task 3 replaces its body per `kind`.
  - `readConfigValue(field)` → the field's current value in PATCH form, or `undefined` meaning "omit this key".

- [ ] **Step 1: Capture the before-baseline**

This task's entire claim is "same controls, same behaviour" — so capture
what the form looks like **now**, before deleting anything. Without a
baseline the claim is unfalsifiable, and pytest cannot check it: the tests
below can only assert that strings appear in the served HTML, never that
the page renders the same.

Start the dev server (README:117):

```bash
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

Mint a throwaway session cookie (never a real credential) and capture:

```bash
COOKIE="$(uv run python -c 'from dashboard import auth; print(auth.create_session_token(remember=False))')"
uv run --no-project python .claude/skills/ui-visual-review/screenshot_ui.py \
  http://127.0.0.1:8000/ /tmp/cfgform-before \
  --cookie "dashboard_session=$COOKIE"
```

Open the Environment tab before capturing — `#configForm` lives there, and
the default landing panel is Status. Keep `/tmp/cfgform-before` until
Step 8.

- [ ] **Step 2: Write the failing test**

Replace `test_dashboard_page_declares_all_17_config_panel_field_ids`
(`dashboard/tests/test_dashboard_page.py:419`) with:

```python
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
    # replaces the 17 verbatim copies.
    assert "infoIconHtml" in body
    assert body.count('data-action="info"') <= 2, (
        "config rows should build their info icons from infoIconHtml(), "
        "not repeat the block per field"
    )
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest dashboard/tests/test_dashboard_page.py -k config_panel_fields -v`
Expected: FAIL — `assert "renderConfigForm" in body`.

- [ ] **Step 4: Delete the hand-written rows**

In `dashboard/static/dashboard.html`, delete everything inside `<form
id="configForm">` from the first `<label>` after `<div id="slotConfigRows"
...></div>` through the final `<input id="cfgUsageCapTokens" type="number">`
— i.e. the whole alphabetised block and the comment at `:683-693` that
documents the ordering this design retires. The form becomes:

```html
        <form id="configForm">
          <!-- Rows are generated from CONFIG_FIELDS by renderConfigForm().
               Ordering is by group (see the registry's `group` field), which
               deliberately replaces the former alphabetical-by-key ordering:
               grouping is what lets the two cross-field rules (base <= max,
               in both the cooldown trio and the backoff pair) be shown at
               all. See docs/superpowers/specs/2026-09-11-dashboard-typed-
               config-controls-design.md section 5. -->
          <div id="providerModelRows" class="provider-model-grid"></div>
          <div id="slotConfigRows" class="provider-model-grid"></div>
        </form>
```

Keep `#providerModelRows` and `#slotConfigRows` exactly as they are — they
are populated by `renderProviderModelRows`/`renderSlotConfigRows` and are
out of this design's scope.

- [ ] **Step 5: Add the generators**

Add near `renderProviderModelRows`:

```js
    // One shared info icon + tooltip, replacing the 17 verbatim copies the
    // hand-written rows used to carry.
    function infoIconHtml(descKey) {
      return `
        <span class="info-wrap">
          <button type="button" class="info-icon" data-action="info"
                  aria-label="More info" data-i18n-aria="env_info_label">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="9"/>
              <line x1="12" y1="11" x2="12" y2="16"/>
              <circle cx="12" cy="7.5" r="0.75" fill="currentColor" stroke="none"/>
            </svg>
          </button>
          <span class="info-tooltip" role="tooltip" data-i18n="${descKey}"></span>
        </span>`;
    }

    // Task 3 replaces this body with one branch per `kind`. For now every
    // field renders exactly the control the hand-written markup used, so this
    // task is a pure refactor with no visual change.
    function controlHtml(field) {
      const id = tuningKnobFieldId(field.key);
      if (field.kind === "enum") {
        return `<select id="${id}">
            <option value="">—</option>
            <option value="gemini">gemini</option>
            <option value="groq">groq</option>
            <option value="vertex">vertex</option>
          </select>`;
      }
      if (field.kind === "bool") {
        return `<input id="${id}" type="checkbox">`;
      }
      if (field.kind === "wallclock") {
        return `<input id="${id}" type="text" placeholder="HH:MM">`;
      }
      const step = field.integer ? "1" : (field.step || "any");
      const min = field.min === undefined ? "" : ` min="${field.min}"`;
      return `<input id="${id}" type="number" step="${step}"${min}>`;
    }

    function configRowHtml(field) {
      return `
        <label for="${tuningKnobFieldId(field.key)}">
          <span data-i18n="env_config_${field.key}"></span>
          ${infoIconHtml("cfg_desc_" + field.key)}
        </label>
        <span class="cfg-cell">${controlHtml(field)}</span>`;
    }

    function renderConfigForm() {
      const form = document.getElementById("configForm");
      const providerRows = document.getElementById("providerModelRows");
      const slotRows = document.getElementById("slotConfigRows");
      const html = CONFIG_FIELDS.filter((f) => f.kind !== "enum")
        .map(configRowHtml)
        .join("");
      // provider is pinned above the generated rows because it drives
      // #providerModelRows directly beneath it.
      form.innerHTML =
        configRowHtml(fieldByKey("provider")) +
        '<div id="providerModelRows" class="provider-model-grid"></div>' +
        '<div id="slotConfigRows" class="provider-model-grid"></div>' +
        html;
      // Re-attach the two containers' previously rendered content, if any.
      if (providerRows) document.getElementById("providerModelRows").innerHTML = providerRows.innerHTML;
      if (slotRows) document.getElementById("slotConfigRows").innerHTML = slotRows.innerHTML;
      form.querySelectorAll('[data-action="info"]').forEach(wireInfoIcon);
      applyLanguage(currentLang);
    }
```

Add the cell style beside the `#configForm` rules (`:299-306`):

```css
  .cfg-cell { display: flex; flex-direction: column; gap: 0.25rem; min-width: 0; }
  .cfg-cell .cfg-line { display: flex; flex-wrap: wrap; align-items: center; gap: 0.45rem; }
```

- [ ] **Step 6: Drive populate and save from the registry**

Replace `populateConfigForm` (`:2592-2608`) and the body-building half of
`saveConfig` (`:2621-2641`):

```js
    function writeFieldValue(field, value) {
      const el = document.getElementById(tuningKnobFieldId(field.key));
      if (field.kind === "bool") {
        el.checked = Boolean(value);
        return;
      }
      el.value = value === null || value === undefined ? "" : value;
    }

    function readConfigValue(field) {
      const el = document.getElementById(tuningKnobFieldId(field.key));
      if (field.kind === "bool") return el.checked;
      if (field.kind === "enum") return el.value || null;
      const raw = el.value;
      if (raw === "") {
        // Blank means "don't touch this field", NOT null, for the fields with
        // no fallback: a NULL column stops the dispatcher dead. Returning
        // undefined makes saveConfig omit the key entirely, which is what
        // _apply_config_patch's exclude_unset already treats as "not sent".
        return TUNING_KNOB_FIELDS.includes(field.key) ? undefined : null;
      }
      if (field.kind === "wallclock") return raw;
      return INTEGER_KNOB_FIELDS.has(field.key) || field.integer
        ? parseInt(raw, 10)
        : parseFloat(raw);
    }

    function populateConfigForm(cfg) {
      CONFIG_FIELDS.forEach((field) => writeFieldValue(field, cfg[field.key]));
      currentConfig = { key_index: cfg.key_index || {} };
      invalidConfigFields.clear();
      updateSaveGuards();
      renderProviderModelRows();
      renderSlotConfigRows(cfg.slot_configs);
    }
```

and in `saveConfig`, replace the literal body object and the
`TUNING_KNOB_FIELDS.forEach` block with:

```js
      const body = { key_index: {} };
      CONFIG_FIELDS.forEach((field) => {
        const value = readConfigValue(field);
        if (value !== undefined) body[field.key] = value;
      });
```

Leave the `.cfg-key-slot` loop below it untouched.

Finally, call the renderer once at load, immediately before
`applyLanguage(currentLang);` at the end of the script:

```js
    renderConfigForm();
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest dashboard/tests/ tests/test_config_field_registry.py -v`
Expected: PASS, including
`test_config_form_omits_blank_tuning_knob_fields_from_the_patch_body`.

- [ ] **Step 8: Prove the refactor changed nothing visually**

Re-capture into a second directory and compare against the baseline:

```bash
uv run --no-project python .claude/skills/ui-visual-review/screenshot_ui.py \\
  http://127.0.0.1:8000/ /tmp/cfgform-after \\
  --cookie "dashboard_session=$COOKIE"
```

Read each pair (`light-desktop`, `dark-desktop`, `mobile`) from both
directories and confirm the config form is **visually identical**. Row
order, label text, control widths and the two-column grid must all match.

Any difference here is a bug in this task, not a preview of Task 3 — the
typed controls arrive in the next task, deliberately in a separate diff.
The one expected difference is that `#providerModelRows` and
`#slotConfigRows` are re-rendered rather than server-emitted; confirm they
still show their key-slot and per-slot model rows and are not empty.

If they differ, fix and re-capture before committing.

- [ ] **Step 9: Commit**

```bash
git add dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py
git commit -m "Generate the config form from the registry

Pure refactor: same controls, same behaviour, ~300 lines of duplicated
markup and three hand-enumerated field lists replaced by one iteration over
CONFIG_FIELDS. Control shapes change in the next commit, separately, so the
structural diff and the visual diff stay reviewable apart."
```

---

### Task 3: Typed control kinds and `syncField`

**Files:**
- Modify: `dashboard/static/dashboard.html`

**Interfaces:**
- Consumes: `controlHtml`, `writeFieldValue`, `readConfigValue` (Task 2).
- Produces:
  - `syncField(key, value)` — the **only** way any code sets a field's value.
  - `dur(seconds)` → `string` — humanised duration ("45s", "5m", "1h").
  - `durLong(seconds)` → `string` — full-precision total ("1m 2s").
  - `humanMagnitude(n)` → `string` — "10k", "1M".
  - `syncAllFields()` — re-syncs every field; called after populate.

- [ ] **Step 1: Write the failing test**

Add to `dashboard/tests/test_dashboard_page.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest dashboard/tests/test_dashboard_page.py -k "typed_not_generic or sync_field" -v`
Expected: FAIL on `assert "function dur(" in body`.

- [ ] **Step 3: Add the formatters and `syncField`**

`syncField` calls `validateField` (Task 4) and `refreshGroupPreview`
(Task 5). Both must exist as no-op stubs from this task, or the page throws
`ReferenceError` on first populate and every later task starts from a broken
page. Add these two lines first, and delete each one in the task that
implements it for real:

```js
    // Stub -- implemented in the grouping/validation task.
    function validateField() {}
    // Stub -- implemented in the previews task.
    function refreshGroupPreview() {}
```

```js
    function trimNum(n) { return (Math.round(n * 10) / 10).toString(); }

    // Humanised duration for a readout: "45s", "5m", "1h". Display only --
    // the wire format is ALWAYS seconds and is never derived from this.
    function dur(seconds) {
      if (!isFinite(seconds) || seconds < 0) return "";
      if (seconds === 0) return "0" + t("unit_abbr_seconds");
      if (seconds >= 3600) return trimNum(seconds / 3600) + t("unit_abbr_hours");
      if (seconds >= 60) return trimNum(seconds / 60) + t("unit_abbr_minutes");
      return trimNum(seconds) + t("unit_abbr_seconds");
    }

    // Full-precision total: 62 reads "1m 2s", never "1m". A preview whose
    // purpose is accuracy may not round.
    function durLong(seconds) {
      if (!isFinite(seconds) || seconds < 0) return "";
      if (seconds < 60) return trimNum(seconds) + t("unit_abbr_seconds");
      const mins = Math.floor(seconds / 60);
      const rest = Math.round((seconds - mins * 60) * 10) / 10;
      if (mins >= 60) {
        const hours = Math.floor(mins / 60);
        return hours + t("unit_abbr_hours") + " " + (mins - hours * 60) + t("unit_abbr_minutes");
      }
      return mins + t("unit_abbr_minutes") + (rest ? " " + rest + t("unit_abbr_seconds") : "");
    }

    function humanMagnitude(n) {
      if (!isFinite(n)) return "";
      if (n >= 1e6) return trimNum(n / 1e6) + "M";
      if (n >= 1e3) return trimNum(n / 1e3) + "k";
      return String(n);
    }

    // THE one entry point for setting a field's value. Assigning .value
    // directly fires no input event, which leaves the readout and the
    // valid/invalid verdict stale -- observed as a row reading
    // "0 seconds (1s)". populate, reset-to-default, preset chips and the
    // stepper all route through here.
    function syncField(key, value) {
      const field = fieldByKey(key);
      writeFieldValue(field, value);
      refreshFieldDisplay(field);
      validateField(field);
      refreshGroupPreview(field.group);
    }

    function syncAllFields() {
      CONFIG_FIELDS.forEach((field) => {
        refreshFieldDisplay(field);
        validateField(field);
      });
      ["cooldown", "backoff"].forEach(refreshGroupPreview);
    }

    // Updates the readout beside a field. Fields with no readout no-op.
    function refreshFieldDisplay(field) {
      const cell = document.getElementById(tuningKnobFieldId(field.key)).closest(".cfg-cell");
      const readout = cell && cell.querySelector(".cfg-readout");
      if (!readout) return;
      const raw = document.getElementById(tuningKnobFieldId(field.key)).value;
      if (field.kind === "magnitude") {
        readout.textContent = raw === "" ? t("cfg_cap_off") : humanMagnitude(parseInt(raw, 10));
        cell.querySelectorAll(".cfg-presets button").forEach((chip) => {
          chip.setAttribute("aria-pressed", String(chip.dataset.value === raw));
        });
        return;
      }
      readout.textContent = raw === "" ? "" : "(" + dur(parseFloat(raw)) + ")";
    }
```

- [ ] **Step 4: Replace `controlHtml` with the typed kinds**

```js
    function controlHtml(field) {
      const id = tuningKnobFieldId(field.key);
      if (field.kind === "enum") {
        return `<span class="cfg-line"><select id="${id}">
            <option value="">—</option>
            <option value="gemini">gemini</option>
            <option value="groq">groq</option>
            <option value="vertex">vertex</option>
          </select></span>`;
      }
      if (field.kind === "bool") {
        return `<span class="cfg-line"><label class="cfg-suffix">
            <input id="${id}" type="checkbox">
            <span data-i18n="cfg_unit_review_drafts"></span>
          </label></span>`;
      }
      if (field.kind === "wallclock") {
        return `<span class="cfg-line">
            <input id="${id}" type="text" class="cfg-num" inputmode="numeric"
                   pattern="([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?">
            <span class="cfg-suffix" data-i18n="cfg_unit_utc"></span>
          </span>`;
      }
      if (field.kind === "count") {
        return `<span class="cfg-line">
            <span class="cfg-stepper">
              <button type="button" data-step="-1" data-key="${field.key}" aria-label="−">−</button>
              <input id="${id}" class="cfg-stepper-value" readonly>
              <button type="button" data-step="1" data-key="${field.key}" aria-label="+">+</button>
            </span>
            <span class="cfg-suffix" data-i18n="cfg_unit_${field.unit}"></span>
          </span>`;
      }
      if (field.kind === "magnitude") {
        const chips = field.presets
          .map((v) => `<button type="button" data-value="${v}" data-key="${field.key}">${humanMagnitude(v)}</button>`)
          .join("");
        return `<span class="cfg-line">
            <input id="${id}" type="number" class="cfg-num" step="1" min="1">
            <span class="cfg-readout"></span>
          </span>
          <span class="cfg-presets">${chips}
            <button type="button" data-value="" data-key="${field.key}" data-i18n="cfg_cap_off_chip"></button>
          </span>`;
      }
      // duration
      const step = field.step === undefined ? "any" : field.step;
      const minAttr = field.exclusive ? ` min="${field.min + 0.001}"` : ` min="${field.min}"`;
      return `<span class="cfg-line">
          <input id="${id}" type="number" class="cfg-num" step="${step}"${minAttr}>
          <span class="cfg-suffix" data-i18n="cfg_unit_${field.unit}"></span>
          ${field.humanize === false ? "" : '<span class="cfg-readout"></span>'}
        </span>`;
    }
```

Extend `configRowHtml` to emit the error and hint lines:

```js
    function configRowHtml(field) {
      const id = tuningKnobFieldId(field.key);
      const noDefault = field.default === null && field.kind === "magnitude";
      const hint = field.default === null && !noDefault
        ? ""
        : noDefault
          ? '<span data-i18n="cfg_hint_no_default"></span>'
          : `<span>${t("cfg_hint_default").replace("{value}", field.default)}</span>
             · <button type="button" class="cfg-reset" data-key="${field.key}"
                       data-i18n="cfg_hint_reset"></button>`;
      return `
        <label for="${id}">
          <span data-i18n="env_config_${field.key}"></span>
          ${infoIconHtml("cfg_desc_" + field.key)}
        </label>
        <span class="cfg-cell">
          ${controlHtml(field)}
          <span class="field-error" data-error-for="${field.key}" hidden></span>
          <span class="field-hint">${hint}</span>
        </span>`;
    }
```

- [ ] **Step 5: Wire the interactive controls**

Append to `renderConfigForm`, after the info-icon wiring:

```js
      form.querySelectorAll(".cfg-stepper button").forEach((btn) => {
        btn.addEventListener("click", () => {
          const field = fieldByKey(btn.dataset.key);
          const current = parseInt(document.getElementById(tuningKnobFieldId(field.key)).value, 10);
          const next = (isFinite(current) ? current : field.min) + parseInt(btn.dataset.step, 10);
          syncField(field.key, Math.max(field.min, next));
        });
      });
      form.querySelectorAll(".cfg-presets button").forEach((chip) => {
        chip.addEventListener("click", () => {
          syncField(chip.dataset.key, chip.dataset.value === "" ? null : parseInt(chip.dataset.value, 10));
        });
      });
      form.querySelectorAll(".cfg-reset").forEach((btn) => {
        btn.addEventListener("click", () => {
          syncField(btn.dataset.key, fieldByKey(btn.dataset.key).default);
        });
      });
      CONFIG_FIELDS.forEach((field) => {
        const el = document.getElementById(tuningKnobFieldId(field.key));
        el.addEventListener("input", () => {
          refreshFieldDisplay(field);
          refreshGroupPreview(field.group);
        });
      });
```

and call `syncAllFields()` at the end of `populateConfigForm`.

- [ ] **Step 6: Add the CSS**

```css
  .cfg-num { width: 7rem; }
  .cfg-suffix { color: var(--text-muted); font-size: 0.85rem; }
  /* Parenthesised, not "= 5m": "=" is bidi-neutral between an RTL run and a
     digit run, so it reorders to the wrong side in Hebrew. Isolation pins the
     whole token. */
  .cfg-readout { color: var(--text-muted); font-size: 0.85rem; unicode-bidi: isolate; }
  .cfg-stepper { display: inline-flex; }
  .cfg-stepper button { padding: 0; width: 1.9rem; height: 1.9rem; line-height: 1; cursor: pointer; }
  .cfg-stepper button + input, .cfg-stepper input + button { border-inline-start: none; }
  .cfg-stepper-value { width: 3rem; text-align: center; padding: 0.4rem 0; }
  .cfg-presets { display: flex; flex-wrap: wrap; gap: 0.3rem; }
  .cfg-presets button { padding: 0.2rem 0.5rem; font-size: 0.8rem; cursor: pointer; }
  .cfg-presets button[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
  .cfg-reset { background: none; border: none; padding: 0; color: var(--text-muted);
    font-size: 0.78rem; text-decoration: underline dotted; cursor: pointer; }
  .cfg-reset:hover { color: var(--accent); }
  @media (max-width: 640px) {
    /* Bumped for touch: the desktop size is ~30px, under the ~44px target,
       and there is no native mobile spinner to fall back to -- verified under
       Playwright mobile emulation, which renders no spinner arrows at all. */
    .cfg-stepper button { width: 2.6rem; height: 2.4rem; font-size: 1rem; }
    .cfg-stepper-value { width: 3.5rem; height: 2.4rem; }
  }
```

- [ ] **Step 7: Run tests and commit**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all PASS.

```bash
git add dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py
git commit -m "Give each config field a control that fits its type

Durations carry a unit and a humanised readout, counts a - n + stepper sized
up for touch on mobile, the token cap presets plus an explicit Off, the reset
time a pattern. Every programmatic write routes through syncField, because
assigning .value fires no input event and leaves the readout stale."
```

---

### Task 4: Grouping, cross-field validation, save guard

**Files:**
- Modify: `dashboard/static/dashboard.html`

**Interfaces:**
- Consumes: `syncField`, `refreshFieldDisplay` (Task 3).
- Produces:
  - `validateField(field)` — per-field bound check; adds/removes from `invalidConfigFields`.
  - `validateGroup(group)` → `string[]` — cross-field problems, keyed by group.
  - `GROUP_ORDER` — `["cooldown", "backoff", "usage_cap", "limits"]`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest dashboard/tests/test_dashboard_page.py -k "groups_fields or cross_field" -v`
Expected: FAIL on `assert "GROUP_ORDER" in body`.

- [ ] **Step 3: Implement validation**

```js
    const GROUP_ORDER = ["cooldown", "backoff", "usage_cap", "limits"];

    function validateField(field) {
      if (field.min === undefined && field.kind !== "wallclock") return;
      const el = document.getElementById(tuningKnobFieldId(field.key));
      const raw = el.value;
      let problem = "";
      if (raw === "") {
        // Blank is legitimate for the magnitude field (cap off) and for the
        // no-fallback knobs (omitted from the PATCH, see readConfigValue).
        problem = "";
      } else if (field.kind === "wallclock") {
        if (!el.checkValidity()) problem = t("cfg_err_wallclock");
      } else {
        const value = parseFloat(raw);
        if (!isFinite(value)) problem = t("cfg_err_not_a_number");
        else if (field.exclusive ? value <= field.min : value < field.min) {
          problem = t("cfg_err_min").replace("{min}", field.min)
            .replace("{rel}", t(field.exclusive ? "cfg_rel_gt" : "cfg_rel_gte"));
        }
      }
      const errorEl = document.querySelector(`[data-error-for="${field.key}"]`);
      if (problem) {
        invalidConfigFields.add(field.key);
        errorEl.hidden = false;
        errorEl.textContent = "* " + problem;
        el.classList.add("cfg-bad");
      } else {
        invalidConfigFields.delete(field.key);
        errorEl.hidden = true;
        el.classList.remove("cfg-bad");
      }
      updateSaveGuards();
    }

    function numberIn(key) {
      const raw = document.getElementById(tuningKnobFieldId(key)).value;
      return raw === "" ? NaN : parseFloat(raw);
    }

    // Cross-field rules. No per-field min/step attribute can express either;
    // the server rejects each group WHOLE, so the client reports it the same
    // way -- one line at the group's foot, keyed by group rather than by
    // field so a rule cannot pass merely because the offending field was not
    // the one blurred.
    function validateGroup(group) {
      const problems = [];
      if (group === "cooldown") {
        const base = numberIn("cooldown_base_seconds");
        const cap = numberIn("cooldown_max_seconds");
        if (isFinite(base) && isFinite(cap) && base > cap) {
          problems.push(t("cfg_err_base_exceeds_max").replace("{base}", base).replace("{max}", cap));
        }
      }
      if (group === "backoff") {
        const base = numberIn("dispatcher_failure_base_backoff_seconds");
        const cap = numberIn("dispatcher_failure_max_backoff_seconds");
        if (isFinite(base) && isFinite(cap) && base > cap) {
          problems.push(t("cfg_err_backoff_base_exceeds_max").replace("{base}", base).replace("{max}", cap));
        }
      }
      const errorEl = document.querySelector(`[data-group-error="${group}"]`);
      if (errorEl) {
        errorEl.hidden = problems.length === 0;
        errorEl.textContent = problems.length ? "* " + problems.join("; ") : "";
      }
      if (problems.length) invalidConfigFields.add("group:" + group);
      else invalidConfigFields.delete("group:" + group);
      updateSaveGuards();
      return problems;
    }
```

Wire blur on every field (validation timing is blur, matching the
render-vars table's existing idiom), inside `renderConfigForm`:

```js
      CONFIG_FIELDS.forEach((field) => {
        const el = document.getElementById(tuningKnobFieldId(field.key));
        el.addEventListener("blur", () => {
          validateField(field);
          validateGroup(field.group);
        });
      });
```

- [ ] **Step 4: Render the groups**

Replace `renderConfigForm`'s row assembly so fields are emitted inside
their group wrappers:

```js
    function groupHtml(group) {
      const rows = CONFIG_FIELDS.filter((f) => f.group === group).map(configRowHtml).join("");
      return `
        <div class="cfg-group" data-group="${group}">
          <div class="cfg-group-cap" data-i18n="cfg_group_${group}"></div>
          <div class="cfg-group-rows">${rows}</div>
          <div class="field-error" data-group-error="${group}" hidden></div>
          <div class="cfg-preview" data-preview-for="${group}" hidden></div>
        </div>`;
    }

    function renderConfigForm() {
      const form = document.getElementById("configForm");
      form.innerHTML =
        configRowHtml(fieldByKey("provider")) +
        '<div id="providerModelRows" class="provider-model-grid"></div>' +
        '<div id="slotConfigRows" class="provider-model-grid"></div>' +
        GROUP_ORDER.map(groupHtml).join("") +
        configRowHtml(fieldByKey("review_draft_prs"));
      form.querySelectorAll('[data-action="info"]').forEach(wireInfoIcon);
      // ... stepper / preset / reset / input / blur wiring from Task 3 ...
      applyLanguage(currentLang);
    }
```

CSS:

```css
  .cfg-group { grid-column: 1 / -1; border: 1px solid var(--border);
    padding: 0.75rem 0.85rem; margin: 0.3rem 0; }
  .cfg-group-cap { font-family: var(--pixel-font); font-size: 0.6rem;
    letter-spacing: 0.03em; text-transform: uppercase;
    color: var(--text-muted); margin-bottom: 0.7rem; }
  .cfg-group-rows { display: grid; grid-template-columns: max-content 1fr;
    gap: 0.6rem 0.75rem; align-items: start; }
  @media (max-width: 640px) { .cfg-group-rows { grid-template-columns: 1fr; } }
  input.cfg-bad { outline: 2px solid var(--fail); outline-offset: 1px; }
```

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest -v && uv run ruff check .`

```bash
git add dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py
git commit -m "Group the config fields and validate across them

Grouping is what lets the two cross-field rules (base <= max, in both the
cooldown trio and the backoff pair) be shown at all -- no per-field attribute
can express either. Rules are keyed by group, not by field, so one cannot
pass merely because the offending field was not the one blurred. This also
feeds invalidConfigFields, which has been declared and inert since it was
written."
```

---

### Task 5: Live previews

**Files:**
- Modify: `dashboard/static/dashboard.html`
- Modify: `tests/test_config_field_registry.py`

**Interfaces:**
- Consumes: `dur`, `durLong`, `validateGroup` (Tasks 3–4).
- Produces: `refreshGroupPreview(group)` — already called by `syncField`
  (Task 3) as a no-op until now.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_field_registry.py`:

```python
def test_cooldown_preview_pins_the_real_level_ceiling():
    """The preview mirrors store.effective_cooldown, whose exponent is clamped
    at _MAX_COOLDOWN_LEVEL. If that constant moves, the preview silently lies
    about where the sequence ends -- so pin it."""
    assert store._MAX_COOLDOWN_LEVEL == 30
    assert "MAX_COOLDOWN_LEVEL = 30" in DASHBOARD_HTML.read_text(encoding="utf-8")


def test_backoff_preview_pins_the_hardcoded_doubling():
    """dispatcher.compute_backoff doubles -- its factor is NOT the configurable
    cooldown factor. If it ever becomes configurable the preview must gain a
    field, and this assertion is what forces that conversation."""
    import inspect

    from review_queue import dispatcher

    source = inspect.getsource(dispatcher.compute_backoff)
    assert "2 ** (attempts - 1)" in source
    assert "BACKOFF_DOUBLING = 2" in DASHBOARD_HTML.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config_field_registry.py -k preview -v`
Expected: FAIL — `assert "MAX_COOLDOWN_LEVEL = 30" in ...`.

- [ ] **Step 3: Implement the previews**

```js
    // Mirrors review_queue/store.py::effective_cooldown --
    //   max(base, min(base * factor ** min(level, 30), cap))
    // The level clamp is what makes the sequence finite: if the cap is never
    // reached, the real ceiling is base * factor**30, not cap.
    const MAX_COOLDOWN_LEVEL = 30;
    // Mirrors review_queue/dispatcher.py::compute_backoff, whose factor is
    // HARDCODED 2 -- not the configurable cooldown factor. Different sequence.
    const BACKOFF_DOUBLING = 2;

    function cooldownAt(level, base, factor, cap) {
      return Math.max(base, Math.min(base * Math.pow(factor, Math.min(level, MAX_COOLDOWN_LEVEL)), cap));
    }

    function chip(text, capped) {
      return `<span class="cfg-chip${capped ? " is-cap" : ""}">${text}</span>`;
    }

    function cooldownPreviewHtml() {
      const base = numberIn("cooldown_base_seconds");
      const factor = numberIn("cooldown_factor");
      const cap = numberIn("cooldown_max_seconds");
      if (![base, factor, cap].every(isFinite)) return "";
      // A constant sequence: factor 1 never escalates, and a base of 0 is
      // equally constant (0 * f**n is 0 at every level) -- and a 0 base is
      // VALID, only a negative one is rejected.
      if (factor === 1 || base === 0) {
        return chip(dur(cooldownAt(0, base, factor, cap)), true) +
          `<span class="cfg-chip-tail" data-i18n="cfg_preview_never_escalates"></span>`;
      }
      let out = "";
      for (let level = 0; level <= MAX_COOLDOWN_LEVEL; level++) {
        const wait = cooldownAt(level, base, factor, cap);
        const atCap = wait >= cap;
        out += chip(dur(wait), atCap);
        if (atCap) {
          return out + `<span class="cfg-chip-tail">${t("cfg_preview_held_at").replace("{value}", dur(cap))}</span>`;
        }
        if (level === 5) {
          const ceiling = dur(cooldownAt(MAX_COOLDOWN_LEVEL, base, factor, cap));
          return out + `<span class="cfg-chip-tail">${t("cfg_preview_ceiling").replace("{value}", ceiling)}</span>`;
        }
      }
      return out;
    }

    function backoffPreviewHtml() {
      const base = numberIn("dispatcher_failure_base_backoff_seconds");
      const cap = numberIn("dispatcher_failure_max_backoff_seconds");
      const jitter = numberIn("dispatcher_backoff_jitter_seconds");
      const attempts = parseInt(document.getElementById(tuningKnobFieldId("dispatcher_max_failure_attempts")).value, 10);
      if (![base, cap].every(isFinite) || !isFinite(attempts)) return "";
      const shown = Math.min(attempts, 8);
      let out = "", total = 0;
      for (let n = 1; n <= attempts; n++) {
        const wait = Math.min(base * Math.pow(BACKOFF_DOUBLING, n - 1), cap);
        total += wait;
        if (n <= shown) out += chip(dur(wait), wait >= cap);
      }
      if (attempts > shown) {
        out += `<span class="cfg-chip-tail">${t("cfg_preview_more").replace("{n}", attempts - shown)}</span>`;
      }
      let tail = t("cfg_preview_gives_up").replace("{total}", durLong(total));
      if (isFinite(jitter) && jitter > 0) {
        tail += t("cfg_preview_jitter").replace("{value}", dur(jitter));
      }
      return out + `<span class="cfg-chip-tail">${tail}</span>`;
    }

    function refreshGroupPreview(group) {
      const host = document.querySelector(`[data-preview-for="${group}"]`);
      if (!host) return;
      // Hide the block ENTIRELY when the group is invalid -- caption included.
      // Clearing the chips but leaving the caption pointing at nothing reads
      // as a rendering fault.
      if (validateGroup(group).length) { host.hidden = true; return; }
      const chips = group === "cooldown" ? cooldownPreviewHtml()
        : group === "backoff" ? backoffPreviewHtml() : "";
      if (!chips) { host.hidden = true; return; }
      host.hidden = false;
      host.innerHTML =
        `<div class="cfg-preview-cap" data-i18n="cfg_preview_cap_${group}"></div>
         <div class="cfg-chips">${chips}</div>`;
      applyLanguage(currentLang);
    }
```

CSS:

```css
  .cfg-preview { margin-top: 0.6rem; padding-top: 0.55rem;
    border-top: 1px dashed var(--border); font-size: 0.82rem; color: var(--text-muted); }
  .cfg-chips { display: flex; flex-wrap: wrap; gap: 0.28rem; align-items: center; margin-top: 0.3rem; }
  .cfg-chip { border: 1px solid var(--border); padding: 0.12rem 0.38rem;
    font-size: 0.8rem; color: var(--text); font-variant-numeric: tabular-nums; }
  .cfg-chip.is-cap { background: var(--ink); color: var(--paper); border-color: var(--ink); }
  .cfg-chip-tail { color: var(--text-muted); font-size: 0.8rem; }
```

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest -v && uv run ruff check .`

```bash
git add dashboard/static/dashboard.html tests/test_config_field_registry.py
git commit -m "Show the sequence each escalating group actually produces

Both previews mirror a Python formula, so the tests pin the constants that
make them true: the level-30 clamp in effective_cooldown, and the fact that
compute_backoff's factor is a hardcoded 2 rather than the configurable
cooldown factor.

The backoff preview earns its place immediately -- at the shipped defaults
the schedule tops out at 32s, so the 300s cap is inert until attempts reach
9. Four number boxes hide that."
```

---

### Task 6: i18n

**Files:**
- Modify: `dashboard/static/dashboard.html` (both `STRINGS.en` and `STRINGS.he`)

**Interfaces:**
- Consumes: every `t("cfg_...")` call from Tasks 3–5.
- Produces: no new functions.

- [ ] **Step 1: Write the failing test**

```python
async def test_every_config_string_key_exists_in_both_languages():
    """Every t("cfg_...") / data-i18n="cfg_..." the config form references must
    resolve in en AND he -- a missing Hebrew key renders as the raw key."""
    client = await _client()
    body = (await client.get("/")).text
    referenced = set(re.findall(r'(?:data-i18n="|t\(")(cfg_[a-z0-9_]+)', body))
    assert referenced, "no cfg_ keys found -- the regex or the markup changed"
    for lang in ("en", "he"):
        block = body.split(f"      {lang}: {{", 1)[1].split("\n      },", 1)[0]
        missing = sorted(k for k in referenced if f"{k}:" not in block)
        assert not missing, f"{lang} is missing: {missing}"
```

Add `import re` at the top of `dashboard/tests/test_dashboard_page.py`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest dashboard/tests/test_dashboard_page.py -k config_string_key -v`
Expected: FAIL listing the missing keys in both languages.

- [ ] **Step 3: Add the English strings**

Into `STRINGS.en`:

```js
        cfg_group_cooldown: "Re-review cooldown",
        cfg_group_backoff: "Failure backoff",
        cfg_group_usage_cap: "Per-key daily usage cap",
        cfg_group_limits: "Timeouts & limits",
        cfg_unit_seconds: "seconds",
        cfg_unit_attempts: "attempts",
        cfg_unit_per_batch: "per batch",
        cfg_unit_each_escalation: "each escalation",
        cfg_unit_utc: "UTC",
        cfg_unit_review_drafts: "review drafts on push",
        unit_abbr_seconds: "s",
        unit_abbr_minutes: "m",
        unit_abbr_hours: "h",
        cfg_hint_default: "default {value}",
        cfg_hint_reset: "reset",
        cfg_hint_no_default: "no default — blank means the cap is off",
        cfg_cap_off: "cap off",
        cfg_cap_off_chip: "Off",
        cfg_rel_gt: "greater than",
        cfg_rel_gte: "at least",
        cfg_err_min: "must be {rel} {min}",
        cfg_err_not_a_number: "must be a number",
        cfg_err_wallclock: "must be HH:MM or HH:MM:SS",
        cfg_err_base_exceeds_max: "base {base} exceeds max {max}",
        cfg_err_backoff_base_exceeds_max: "base backoff {base} exceeds max backoff {max}",
        cfg_preview_cap_cooldown: "Successive re-reviews of the same PR wait:",
        cfg_preview_cap_backoff: "Retry schedule after a hard failure:",
        cfg_preview_held_at: "then held at {value}",
        cfg_preview_ceiling: "reaching {value} at the 31st re-review",
        cfg_preview_never_escalates: "every time — this cooldown never escalates",
        cfg_preview_more: "+{n} more",
        cfg_preview_gives_up: "then gives up — {total} total",
        cfg_preview_jitter: ", plus jitter of up to {value} each",
```

- [ ] **Step 4: Shorten the existing labels and add the Hebrew**

The labels shorten because they now sit under a group caption. **Change
these existing `env_config_*` values in `STRINGS.en`** (the keys stay):

| Key | Was | Becomes |
|---|---|---|
| `env_config_cooldown_base` → `env_config_cooldown_base_seconds` | "Cooldown base (seconds)" | "Base" |
| `env_config_cooldown_factor` | "Cooldown factor" | "Multiply by" |
| `env_config_cooldown_max` → `env_config_cooldown_max_seconds` | "Cooldown max (seconds)" | "Up to" |
| `env_config_dispatcher_failure_base_backoff_seconds` | (long) | "First retry after" |
| `env_config_dispatcher_failure_max_backoff_seconds` | (long) | "Up to" |
| `env_config_dispatcher_backoff_jitter_seconds` | (long) | "Random jitter" |
| `env_config_dispatcher_max_failure_attempts` | (long) | "Give up after" |
| `env_config_usage_cap_tokens` | "Usage cap (tokens)" | "Token cap" |
| `env_config_usage_cap_reset` | "Usage cap reset (UTC)" | "Resets at" |

The registry keys the label as `env_config_<field.key>`, so the three
cooldown keys are **renamed** from `env_config_cooldown_base`/`_factor`/`_max`
to `env_config_cooldown_base_seconds`/`_factor`/`_max_seconds`. Rename them
in both language blocks. Leave the `limits` group's labels as they are —
they already read well standalone.

Mirror every new and changed string into `STRINGS.he`. Hebrew for the new
keys:

```js
        cfg_group_cooldown: "צינון בדיקה חוזרת",
        cfg_group_backoff: "נסיגה לאחר כשל",
        cfg_group_usage_cap: "תקרת שימוש יומית למפתח",
        cfg_group_limits: "פסקי זמן ומגבלות",
        cfg_unit_seconds: "שניות",
        cfg_unit_attempts: "ניסיונות",
        cfg_unit_per_batch: "לכל אצווה",
        cfg_unit_each_escalation: "בכל הסלמה",
        cfg_unit_utc: "UTC",
        cfg_unit_review_drafts: "לבדוק טיוטות בעת push",
        unit_abbr_seconds: " שנ׳",
        unit_abbr_minutes: " דק׳",
        unit_abbr_hours: " ש׳",
        cfg_hint_default: "ברירת מחדל {value}",
        cfg_hint_reset: "איפוס",
        cfg_hint_no_default: "אין ברירת מחדל — ריק מבטל את התקרה",
        cfg_cap_off: "התקרה מבוטלת",
        cfg_cap_off_chip: "מבוטל",
        cfg_rel_gt: "גדול מ-",
        cfg_rel_gte: "לפחות",
        cfg_err_min: "חייב להיות {rel} {min}",
        cfg_err_not_a_number: "חייב להיות מספר",
        cfg_err_wallclock: "חייב להיות בפורמט HH:MM או HH:MM:SS",
        cfg_err_base_exceeds_max: "הבסיס {base} גדול מהמקסימום {max}",
        cfg_err_backoff_base_exceeds_max: "בסיס הנסיגה {base} גדול מהמקסימום {max}",
        cfg_preview_cap_cooldown: "בדיקות חוזרות עוקבות של אותו PR ימתינו:",
        cfg_preview_cap_backoff: "לוח הניסיונות החוזרים לאחר כשל:",
        cfg_preview_held_at: "ומכאן מוחזק על {value}",
        cfg_preview_ceiling: "ומגיע ל-{value} בבדיקה החוזרת ה-31",
        cfg_preview_never_escalates: "בכל פעם — הצינון הזה לעולם אינו מסלים",
        cfg_preview_more: "+{n} נוספים",
        cfg_preview_gives_up: "ואז נוטש — {total} בסך הכול",
        cfg_preview_jitter: ", בתוספת רעש של עד {value} בכל אחד",
```

Hebrew labels for the shortened keys: `env_config_cooldown_base_seconds`
"בסיס", `env_config_cooldown_factor` "מוכפל ב-",
`env_config_cooldown_max_seconds` "עד",
`env_config_dispatcher_failure_base_backoff_seconds` "ניסיון ראשון אחרי",
`env_config_dispatcher_failure_max_backoff_seconds` "עד",
`env_config_dispatcher_backoff_jitter_seconds` "רעש אקראי",
`env_config_dispatcher_max_failure_attempts` "נטישה אחרי",
`env_config_usage_cap_tokens` "תקרת טוקנים", `env_config_usage_cap_reset`
"מתאפס ב-".

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest -v && uv run ruff check .`

```bash
git add dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py
git commit -m "Translate the new config controls, and re-translate what moved

Labels shorten because they now sit under a group caption -- 'Cooldown base
(seconds)' becomes 'Base' under 'Re-review cooldown' -- so this re-translates
existing strings rather than only adding new ones. The duration formatter
takes its unit tokens from the string table too, since s/m/h are not
language-neutral."
```

---

### Task 7: Visual review and close-out

**Files:**
- Modify: `ISSUES.md` (parked findings only, if any)

- [ ] **Step 1: Run the full suite and linter**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all PASS, zero lint errors. Fix anything either finds — never
push with a red suite.

- [ ] **Step 2: Invoke `ui-visual-review`**

**REQUIRED by the root `CLAUDE.md` before any `dashboard/static/` change is
called done.** Capture light-desktop, dark-desktop and mobile.

Task 2 already ran a render check, but a narrow one: it proved the
*refactor* changed nothing. Everything that changes appearance — typed
controls, groups, previews, shortened labels, Hebrew — landed in Tasks 3–6
and is unrendered until now. This is the first look at the actual design.

**Additionally capture RTL**, which the skill's default set does not cover:
this design introduces the first bidi-sensitive content in the config form,
and an `= 5m` readout was verified broken in RTL during design (`=` is
bidi-neutral between an RTL run and a digit run, so it reorders). Confirm
the readout renders as `(5m)` on the correct side.

Check specifically:
- stepper buttons reach ~44px under 640px
- the hint line reads `default 300 · reset`, not `default 300 reset`
- a group whose cross-field rule fails hides its **whole** preview block, caption included
- no row shows a readout that disagrees with its input

- [ ] **Step 3: Log any parked findings**

Any Minor finding from review that is not being fixed goes into
`ISSUES.md`'s Parked Issues section with a **Why parked** line — including
findings judged "no action needed", per the root `CLAUDE.md`. Do not leave
them only in the ledger.

- [ ] **Step 4: Commit and finish**

```bash
git add -A
git commit -m "Record visual review results and park remaining findings"
```

If this branch is to reach `main`, invoke `deploy-verify` before pushing —
a green pytest/ruff run does not substitute for it.

---

## Self-Review Notes

Spec coverage checked section by section: §3 registry → Task 1; §4 control
kinds → Tasks 2–3; §5 grouping and previews → Tasks 4–5; §6 validation and
`syncField` → Tasks 3–4; §7 tests → Tasks 1, 5, 6; §8 i18n → Task 6; §9
visual review → Task 7; §10 out-of-scope items are touched by no task.

Naming consistency verified across tasks: `syncField`, `validateField`,
`validateGroup`, `refreshFieldDisplay`, `refreshGroupPreview`,
`readConfigValue`, `writeFieldValue`, `fieldByKey`, `numberIn`, `dur`,
`durLong`, `humanMagnitude`, `tuningKnobFieldId` are each defined once and
referenced with the same signature everywhere.

Render checkpoints: Task 2 (before/after comparison, proving the pure
refactor is pure) and Task 7 (full review of the design). The gap this
closes is that Task 2 deletes ~300 lines of markup and rebuilds the form in
JS, while pytest can only assert strings appear in the served HTML — it
cannot execute the JS or see the result.

Known ordering constraint: `refreshGroupPreview` is called by `syncField`
in Task 3 but only implemented in Task 5. Task 3 must therefore define it
as a no-op stub (`function refreshGroupPreview() {}`) which Task 5 replaces
— otherwise Task 3 leaves the page throwing `ReferenceError` on first
populate. The same applies to `validateField`, stubbed in Task 3 and
implemented in Task 4.
