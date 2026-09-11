# Typed config controls for the dashboard's runtime_config form

Picks up the client-side half that
`2026-09-10-cross-repo-contract-direction-design.md` §4.4 deliberately
parked:

> Client-side input attributes (`pattern` on the reset field, `min` on the
> cooldown and token-cap fields) are deliberately **not** in this design.
> Server-side validation is the correctness fix; the inputs are a UI
> concern to be brainstormed separately.

That parked item turned out to be larger than "add three attributes", for
a reason §4.4 could not have known: hand-written attributes are a
duplicate of the server predicates, and one of them is **already wrong**
today (§1.2). This design is therefore about where that duplicate lives
and what stops it drifting, as much as it is about control shapes.

Server-side validation is unchanged by this design. `cooldown_config.
problems()`, `usage_cap_config.problems()` and `dispatcher_tuning_config.
problems()` remain the sole authority on validity; nothing here may
weaken, bypass, or re-implement their verdict as the thing that decides
whether a write is allowed.

## 1. Problem

### 1.1 Every field renders as the same generic box

`dashboard/static/dashboard.html`'s `#configForm` renders all 17
`runtime_config`-backed fields as `<input type="number">` or
`<input type="text">` regardless of the field's actual semantic type:

| Field | Real constraint (source of truth) | Input today |
|---|---|---|
| `cooldown_base_seconds` | `>= 0` **and `<= max`** (`cooldown_config.py:39-45,55-58`) | `type=number step=any`, no `min` (`:708`) |
| `cooldown_max_seconds` | `> 0` | `type=number step=any`, no `min` (`:738`) |
| `cooldown_factor` | `>= 1.0` | `type=number step=any`, no `min` (`:723`) |
| `key_usage_token_cap` | `> 0`, **or null = cap intentionally off** (`usage_cap_config.py:53-76`) | bare `type=number` (`:934`) |
| `key_usage_reset_time_utc` | parses via `time.fromisoformat` | `type=text` + placeholder (`:919`) |
| 8 dispatcher knobs | `>= 0` / `> 0` / `>= 1` (`dispatcher_tuning_config.py:59-73`) | `type=number` + `min` ✅ |
| `dispatcher_failure_base_backoff_seconds` | additionally `<= max backoff` | no cross-field expression possible |
| `dispatcher_idle_sleep_seconds` | `> 0` — checked **only** in `environment.py:868-875`, not in `problems()` | `min="0"` ❌ **wrong** (`:813`) |

A `type="number"` communicates "a number goes here" and nothing else: not
the unit, not the magnitude, not that blank means "cap off" rather than
"broken", and not that three of these fields are one setting.

### 1.2 The hand-copied attribute is already wrong

`dispatcher_idle_sleep_seconds` carries `min="0"` while the server rejects
`0` outright — `environment.py:868-875` calls it out as a value that
"would busy-loop the dispatcher". The client currently invites exactly the
value the server refuses. Nothing detects this: there is no test relating
a client attribute to the predicate it mirrors.

This is the same failure shape as the incidents in the root `CLAUDE.md`'s
cross-repo section — a fact duplicated across a boundary with no mechanism
keeping the copies honest — at a smaller scale.

### 1.3 The client-side validation hook exists and is dead

`dashboard.html:2336` declares `invalidConfigFields`, `:2338-2341`
disables `#saveConfigBtn` while it is non-empty, and `saveConfig()`
(`:2616-2619`) refuses to submit. **Nothing ever adds to the set.** The
guard is fully built and permanently inert. Its sibling
`invalidRenderFields` is live, fed by a blur handler that posts to
`/api/environment/validate/{var}` (`:1908-1930`).

### 1.4 Markup duplication makes per-field treatment expensive

Each of the 17 rows repeats the same 14-line `<label>` + info-icon SVG
block verbatim (`:694-935`) — roughly 300 lines of near-identical markup.
Field lists are hand-enumerated in three further places
(`TUNING_KNOB_FIELDS` `:2563-2576`, `INTEGER_KNOB_FIELDS` `:2583-2587`,
and `populateConfigForm` `:2592-2601`). Any per-field improvement costs
another copy in every one of them.

## 2. Confirmed decisions

Reached by rendering labelled candidates in the real one-bit palette and
judging them at desktop, mobile, light, dark and RTL, rather than from
description. Scratchpad comps are throwaway; the findings are not.

1. **Declarative field registry**, not per-field one-offs (§3).
2. **Probing conformance test** as the anti-drift mechanism — not a
   generated artifact, not a runtime schema endpoint (§7).
3. **Control kinds**: `wallclock` = text + `pattern` + inline validity;
   `magnitude` = number + preset chips + explicit Off; `duration` = number
   + unit suffix + humanised readout; `count` = `− n +` stepper; `bool` =
   checkbox; `enum` = the existing `<select>` (§4).
4. **Grouping with live previews** for the two escalating-sequence groups
   (§5).
5. **Every row carries a hint line** showing its declared default (§4.7).
6. **Blur-triggered validation** (§6).

### 2.1 Rejected, with reasons

- **`<input type="time">`** for the reset field. Free validity and a real
  picker, but the picker popup and spinner glyphs are browser chrome —
  rounded, system-coloured, unreachable by our CSS — against a design
  system (`DESIGN.md`) whose stated commitment is a one-bit material with
  no soft or ambient depth.
- **Stepped log slider** for the token cap. Reads scale well, but can only
  produce its own detents (no arbitrary 750k), needs bespoke
  `::-webkit-slider-*` restyling per engine, and dragging is worse than
  tapping on mobile. Preset chips deliver the same "typical choices are
  one tap" benefit with no new material.
- **Unit `<select>`** (seconds/minutes/hours) on duration fields. Nicest
  to type into, but round-trips lossily: a stored `90` has no exact
  minutes form, so re-opening the form must guess a unit. The wire format
  stays pure seconds; the humanised value is display-only.
- **Native `<input type="number">` spinners** for counts. Verified under
  Playwright mobile emulation (`is_mobile=True, has_touch=True`): **no
  spinner arrows render at all**. There is no native mobile affordance to
  prefer, so the choice was between a custom stepper and typing alone.

## 3. The registry

One table in `dashboard.html` is the single client-side description of
every `runtime_config`-backed field:

```js
const CONFIG_FIELDS = [
  { key: "cooldown_base_seconds", kind: "duration", group: "cooldown",
    min: 0, exclusive: false, step: "any", unit: "seconds", default: 300 },
  { key: "cooldown_factor", kind: "duration", group: "cooldown",
    min: 1, exclusive: false, step: 0.5, unit: "each_escalation", default: 2 },
  { key: "key_usage_token_cap", kind: "magnitude", group: "usage_cap",
    min: 1, exclusive: false, nullable: "cap_off", default: null,
    presets: [10000, 100000, 500000, 1000000, 5000000] },
  { key: "key_usage_reset_time_utc", kind: "wallclock", group: "usage_cap",
    default: "04:00:00" },
  { key: "dispatcher_idle_sleep_seconds", kind: "duration", group: "limits",
    min: 0, exclusive: true, step: "any", unit: "seconds", default: 1 },
  // ... 17 entries total
];
```

Field meanings:

- `key` — the `runtime_config` column / PATCH body key. Also derives the
  DOM id (`cfg` + PascalCase, the existing `tuningKnobFieldId` convention)
  and the i18n keys (`env_config_<key>`, `cfg_desc_<key>`).
- `kind` — which control renders (§4).
- `group` — which titled group it sits in (§5).
- `min` / `exclusive` — the numeric bound and whether it is strict. These
  are the two values §7's test probes against the real predicate.
- `step`, `integer`, `unit`, `default`, `presets`, `nullable` —
  presentation and wire details.

It drives, replacing the hand-enumerated copies named in §1.4:

1. Markup generation for all 17 rows, including the info-icon block.
2. Control selection and its attributes.
3. Client-side validation → `invalidConfigFields`.
4. `populateConfigForm` — iterate the registry, not a literal list.
5. `saveConfig`'s PATCH body, including the existing "blank means omit the
   key entirely, never send null" rule for fields with no fallback
   (`:2632-2639` — that comment's reasoning is preserved verbatim, as it
   encodes why a NULL column stops the dispatcher dead).

### 3.1 Split of responsibility

The registry has two halves that behave differently and must not be
conflated:

- **Derivable half** (`min`, `exclusive`, `integer`, `default`) — already
  exists in Python. Policed by §7's test.
- **Presentation half** (`kind`, `group`, `unit`, `presets`, ordering) —
  has no server-side counterpart and should not acquire one. It is a UI
  judgement, and inventing a server field to hold it would put
  presentation concerns into a module whose job is validity.

## 4. Control kinds

Every kind renders the same row skeleton, so the grid stays uniform:

```
label │ control-line
      │ error-line   (hidden until invalid)
      │ hint-line    (default + reset)
```

`#configForm`'s two-column grid (`:299-306`) keeps its `max-content 1fr`
shape; the second cell becomes a wrapper holding control, error and hint.
The existing single-column collapse under 640px (`:458-462`) continues to
stack label above control, with the label bolded.

### 4.1 `duration` — 9 fields ending `_seconds`, plus `cooldown_factor`

`<input type="number">` + a unit suffix + a parenthesised humanised
readout that updates as you type:

```
Cooldown max   [ 3600 ] seconds  (1h)
               default 3600 · reset
```

The unit moves out of the label into its own element, which shortens
every label and helps the Hebrew column. The readout is display-only —
**the wire format is always seconds** and is never derived from it.

`cooldown_factor` is a `duration`-shaped numeric without a time unit; its
suffix reads "each escalation" and its `step` is `0.5`, so the native
stepper walks 1, 1.5, 2, 2.5 from its `min` of 1.

### 4.2 `count` — 3 integer fields

A `− n +` stepper, horizontal rather than stacked:

```
Give up after  [ − ][ 5 ][ + ] attempts
```

Sizing is responsive: compact on desktop, buttons bumped to ≈2.4rem under
640px. This is the one place the design deliberately looks chunkier on
mobile than desktop. The justification is measured, not aesthetic — at
the desktop size each half is ~17 CSS px against a ~44 px touch target,
and §2.1 establishes there is no native spinner to fall back to.

The value cell is `readonly`, so the stepper is the only way to change it
and no unparseable text can be typed.

### 4.3 `magnitude` — `key_usage_token_cap`

A number input, a row of preset chips, and an explicit **Off** chip:

```
Token cap   [ 1000000 ] 1M
            [10k][100k][500k][1M][5M][Off]
            no default — blank means the cap is off
```

The chips make typical magnitudes one tap while the exact number stays
editable. **Off** renders "cap intentionally disabled" as a visible,
selectable state rather than an empty box indistinguishable from a
mistake — matching `usage_cap_config`'s own docstring, which is explicit
that a null cap paired with a real reset time is a valid configured
state, not "unset".

This is the one field with no declared default
(`runtime_config_defaults.NO_DEFAULT_BY_DESIGN`), so its hint line says so
instead of naming a value, and it has no `reset` action.

### 4.4 `wallclock` — `key_usage_reset_time_utc`

`<input type="text">` with `inputmode="numeric"` and
`pattern="([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?"`, validated on blur
via `checkValidity()`, with an inline error line.

The pattern is a fourth place the format is written down (after the
column's TEXT type, `time.fromisoformat`, and
`runtime_config_defaults`'s "always 3-part" note). §7.3 covers what keeps
it honest.

### 4.5 `bool` — `review_draft_prs`

Unchanged behaviour; gains only the hint line.

### 4.6 `enum` — `provider`

The existing `<select>`, unchanged in markup and behaviour. It is a
registry entry so the 17 are described in one place and §7.1's coverage
assertion can be exhaustive, but it has no `min`/`exclusive` to probe and
keeps its pinned position above the groups (§5) because it drives
`#providerModelRows` directly beneath it.

The 17 fields distribute as: 10 `duration` (the 9 ending `_seconds`, plus
`cooldown_factor`), 3 `count`, 1 `magnitude`, 1 `wallclock`, 1 `bool`,
1 `enum`.

### 4.7 Hint line

Every row carries one, reading `default <value> · reset`, where `reset`
is a dotted-underline button restoring the declared default. The
separator is required: without it the line reads as the single phrase
"default 300 reset".

Defaults come from the registry, which §7 pins to
`runtime_config_defaults.COLUMN_TO_SETTING`'s declared values — already
published non-secretly in `contracts/provisioning.json`, so this adds no
new source of truth.

`reset` writes through `syncField()` (§6.2), never by assigning `.value`
directly.

## 5. Grouping

Four titled groups. The classes are new, but they follow the ruled-hairline
idiom `.provider-model-row` already establishes (`dashboard.html:318-331`) --
dashed separators between rows, no nested filled or dithered panel, per
`DESIGN.md`'s rejection of ambient depth.

| Group | Fields |
|---|---|
| Re-review cooldown | `cooldown_base_seconds`, `cooldown_factor`, `cooldown_max_seconds` |
| Failure backoff | `dispatcher_failure_base_backoff_seconds`, `dispatcher_failure_max_backoff_seconds`, `dispatcher_backoff_jitter_seconds`, `dispatcher_max_failure_attempts` |
| Per-key daily usage cap | `key_usage_token_cap`, `key_usage_reset_time_utc` |
| Timeouts & limits | `llm_request_timeout_seconds`, `dispatcher_default_retry_after_seconds`, `dispatcher_min_retry_after_seconds`, `dispatcher_idle_sleep_seconds`, `dispatcher_notice_sweep_batch_size`, `dispatcher_max_notice_post_attempts` |

`provider` stays pinned above (it drives `#providerModelRows` directly
beneath it); `review_draft_prs` sits below the groups.

**This retires the alphabetical ordering** that `dashboard.html:683-693`
currently documents as deliberate, and **moves
`dispatcher_max_failure_attempts` out of alphabetical position** to sit
beside the backoff fields whose schedule length it controls. Both are
intentional consequences of grouping; that comment block is replaced, not
left contradicting the code.

Grouping is what lets the two **cross-field** rules be expressed at all —
`base <= max` exists in both the cooldown trio (`cooldown_config.py:
55-58`) and the backoff pair (`dispatcher_tuning_config.py:88-91`), and
no per-field `min`/`step` attribute can state either. Each group renders
one error line at its foot when its cross-field rule fails, mirroring the
server, which rejects these groups **whole** and never partially writes
them (`environment.py:794-806, 861-878`).

### 5.1 Live previews

Two groups compute the sequence their fields describe.

**Re-review cooldown** mirrors `store.effective_cooldown`
(`store.py:409-419`):

```
max(base, min(base * factor ** min(level, 30), cap))
```

```
Successive re-reviews of the same PR wait:
[5m][10m][20m][40m][1h] then held at 1h
```

Two properties of the real formula the preview must not simplify away:

- **The exponent is clamped at `_MAX_COOLDOWN_LEVEL = 30`**
  (`store.py:406`), so the sequence is finite. When the cap is never
  reached, the true ceiling is `base × factor³⁰`, not `cap` — the preview
  names that value rather than trailing off with an ellipsis.
- **The outer `max(base, …)`** means a cap below base yields *base*.
  Unreachable through a valid config, but the preview mirrors the formula
  as written rather than the formula as assumed.

**Constant-sequence collapse.** When the sequence never changes, the
preview shows a single chip reading "every time — this cooldown never
escalates". The condition is `factor === 1 || base === 0`, not `factor
=== 1` alone: `0 × f^n` is `0` at every level, so a zero base is equally
constant, and a zero base is *valid* (`cooldown_config.py:36-38` is
explicit that only a negative base is rejected).

**Failure backoff** mirrors `dispatcher.compute_backoff`
(`dispatcher.py:73-82`):

```
min(base * 2 ** (attempts - 1), cap) + jitter
```

```
Retry schedule after a hard failure:
[2s][4s][8s][16s][32s] then gives up — 1m 2s total
```

Note the factor here is **hardcoded `2`**, not the configurable cooldown
factor — the two sequences are not the same shape and the preview must
not imply they are. Its term count is `dispatcher_max_failure_attempts`,
a field in the same group, so changing the stepper visibly lengthens the
schedule. Non-zero jitter is annotated ("plus 0–Ns jitter each") rather
than folded into the chips, since it is random per retry.

The total is rendered at full precision: 62 seconds reads "1m 2s", never
"1m". A preview whose purpose is accuracy may not round.

The backoff preview earned its place during design by exposing a real
fact about the shipped defaults: at base 2, ×2, 5 attempts the schedule
tops out at 32s, so `dispatcher_failure_max_backoff_seconds = 300` is
**inert** — it does not bind until attempts reach 9. Four independent
number boxes hide that; the ladder states it.

Both previews hide **entirely** when their group is invalid — caption
included. Clearing the chips while leaving the caption pointing at
nothing reads as a rendering fault.

## 6. Validation and save guard

### 6.1 Timing and wiring

Validation runs on **blur**, matching the render-vars table's existing
idiom (`:1908-1930`). A failing field adds its key to
`invalidConfigFields` (§1.3) and renders its error line; passing removes
it. `updateSaveGuards()` then disables `#saveConfigBtn`, and
`saveConfig()`'s existing pre-flight refusal (`:2616-2619`) becomes
reachable for the first time.

Group-level cross-field rules are evaluated on blur of **any** member and
key the group, not the field, so a rule is never reported as passing
because the field that would have failed it was not the one touched.

This is a convenience layer only. The server's `problems()` predicates
still run on every PATCH and remain the only thing that decides whether a
write happens; a client that skipped validation entirely would be no less
safe, only less pleasant.

### 6.2 One `syncField()` entry point

**Every write to a field's value goes through `syncField(key, value)`,
which sets the value, re-runs the readout, re-runs validation, and
re-renders any group preview the field participates in. No code path
assigns `.value` directly.**

This rule exists because of a bug observed in the design mockup: setting
`dispatcher_idle_sleep_seconds` programmatically left the row reading
`0 seconds (1s)` — a stale readout beside a fresh value — because
assigning `.value` fires no `input` event. Four paths write values
programmatically:

1. `populateConfigForm` on server fetch and on tab re-entry
2. the `reset` button in each hint line (§4.7)
3. preset chips and **Off** (§4.3)
4. the `count` stepper (§4.2)

Each would reproduce the same staleness independently. Routing them
through one function is what makes that structurally impossible rather
than a thing to remember four times.

## 7. Tests

### 7.1 Probing conformance test — the anti-drift mechanism

New `tests/test_config_field_registry.py` parses `CONFIG_FIELDS` out of
`dashboard.html` and, for each entry, **probes the real Python predicate
at its boundary**:

```python
for field in parse_registry(DASHBOARD_HTML):
    p = predicate_for(field.key)          # from the real _BOUNDS tuples
    if field.exclusive:
        assert not p(field.min)
        assert p(field.min + EPS)
    else:
        assert p(field.min)
        assert not p(field.min - EPS)
    assert field.default == declared_default(field.key)
```

`predicate_for` resolves a key against the three `_BOUNDS` tuples, with one
documented exception: `dispatcher_idle_sleep_seconds` has **no `_BOUNDS`
entry at all** — its rule lives as inline code in `environment.py:868-875`
(§10 explains why). The test states that predicate explicitly and
comments that it is transcribed from there, so consolidating it server-side
later is a one-line change here rather than a silent divergence.

Probing rather than reading is required: the predicates are opaque
lambdas (`lambda v: v >= 0`), so no bound *number* can be extracted from
them mechanically — but any bound can be confirmed by evaluating the
lambda either side of it.

The test reads the registry, not the markup, so it cannot literally fail
against today's tree -- there is no registry yet. What it does is make
§1.2's bug **unwritable**: transcribing the current `min="0"` for
`dispatcher_idle_sleep_seconds` into the registry fails the probe
immediately, because the predicate rejects `0`. Correcting that bound is
part of this work; the test is what stops it recurring.

Registry coverage is asserted too: every key in `store.RUNTIME_CONFIG_COLUMNS` (`store.py:44`)
that the form edits has exactly one registry entry, and no entry names a
column that does not exist.

### 7.2 Preview formula pinning

The two previews duplicate Python formulas in JS. The test pins the
constants that make them true, so a Python-side change fails CI loudly
instead of silently making the dashboard lie:

- `store._MAX_COOLDOWN_LEVEL == 30`, and the registry's preview mirrors
  that literal.
- `compute_backoff`'s factor is `2` and is not read from config — if it
  ever becomes configurable, the backoff preview must gain a field, and
  this assertion is what forces that conversation.

### 7.3 Reset-time pattern

The `pattern` regex is asserted to accept and reject the same strings
`time.fromisoformat` does, over a table of cases spanning both — `04:00`,
`04:00:00`, `24:00`, `4:00`, `04:60`, `""`. This closes the gap left by
the pattern being a fourth statement of the format (§4.4).

### 7.4 Existing tests that change

`dashboard/tests/test_dashboard_page.py:419`
(`test_dashboard_page_declares_all_17_config_panel_field_ids`) asserts
literal `id="cfg…"` strings in the served HTML. With rows generated from
the registry, those ids no longer appear as literals. The test is
rewritten to assert the **registry** declares all 17 keys, which is the
same guarantee against the new structure.
`test_config_form_omits_blank_tuning_knob_fields_from_the_patch_body`
(`:452`) must keep passing unchanged — the omit-don't-null rule survives
the refactor intact.

### 7.5 Not covered by tests

Rendering. Whether the stepper is actually thumb-reachable, whether the
hint lines make the form too tall, whether RTL mirrors correctly — none
of it is reachable from `pytest`. That is what §9 is for.

## 8. i18n

Larger than an additive change:

- **~20 new keys** — 4 group captions, unit suffixes (`seconds`,
  `attempts`, `per batch`, `each escalation`, `UTC`), the hint template
  `default {value}`, `reset`, 2 preview captions, preview tails (`then
  held at {v}`, `then gives up — {v} total`, `every time — this cooldown
  never escalates`, `plus 0–{v} jitter each`, `reaching {v} at the 31st
  re-review`), `cap off`, `no default — blank means the cap is off`, and
  the client-side error strings.
- **Existing `env_config_*` values change.** Labels shorten inside a
  titled group: "Cooldown base (seconds)" becomes "Base" under a
  "Re-review cooldown" caption. The Hebrew side needs **re-translation of
  existing strings**, not just new ones.
- **`dur()` takes a locale.** The humanised readout's `s`/`m`/`h` tokens
  are translatable, so the formatter cannot hardcode Latin unit letters.
- **Bidi.** The readout renders as `(5m)` inside an element carrying
  `unicode-bidi: isolate` (equivalently `<bdi>`). An earlier `= 5m` form
  was verified broken in RTL: `=` is a bidi-neutral character between an
  RTL run and a digit run, so it reorders to the wrong side. This affects
  Hebrew, not merely English-text-in-an-RTL-container.

`cfg_desc_*` tooltip descriptions are unchanged in content; only their
attachment point moves into generated markup.

## 9. Visual review is required

Per the root `CLAUDE.md`, the `ui-visual-review` skill must be invoked
before this work is called done — screenshots at light-desktop,
dark-desktop and mobile. **Additionally, RTL must be captured**, because
this design introduces the first bidi-sensitive content in the config
form (§8) and RTL is where the readout bug was found.

Reading the HTML and reasoning about layout does not substitute. Three of
this design's decisions — the stepper touch-target sizing, the readout's
bidi behaviour, the dangling preview caption — were each found by
rendering, and none was visible from source.

## 10. Out of scope

- **Any change to the server-side predicates.** `problems()` in all three
  modules is correct and stays as-is. This design adds a client
  convenience layer over it, never a second opinion about validity.
- **The `dispatcher_idle_sleep_seconds` validation asymmetry.** It is
  checked in `environment.py:868-875` rather than in
  `dispatcher_tuning_config.problems()` because it is read through a
  separate throttled path. The registry records its true bound and §7.1
  probes `environment.py`'s check; consolidating the predicate itself is
  a server-side question for another design.
- **The render-vars table** (`#renderVarsTable`) and its
  `invalidRenderFields` guard. Untouched; the two guards stay separately
  scoped exactly as `:2331-2335` documents.
- **Slot config rows** (`#slotConfigRows`) and the provider/key-slot
  selectors. Their PATCH targets a different endpoint and their controls
  are already fit for purpose.
- **A server-side config-schema endpoint.** Considered and rejected in
  favour of §7.1: it would add a round trip before the form can render
  its own constraints, and the presentation half of the registry (§3.1)
  would still be hand-written regardless.
