---
name: Autonomous Code Review Engine — Dashboard
description: A one-bit windowed desktop for a solo operator to read pipeline health and edit config at a glance.
colors:
  ink: "#16150f"
  paper: "#f1efe6"
  surface: "#faf9f2"
  line: "#16150f"
  muted: "#5c5a4d"
  signal: "#a3550a"
typography:
  display:
    fontFamily: "Press Start 2P, -apple-system, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.6rem"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0.03em"
  body:
    fontFamily: "-apple-system, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.95rem"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "-apple-system, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.85rem"
    fontWeight: 400
rounded:
  none: "0px"
spacing:
  xs: "0.3rem"
  sm: "0.5rem"
  md: "0.85rem"
  lg: "1.25rem"
  xl: "1.5rem"
components:
  window:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0.85rem 1rem"
  window-title:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.display}"
    rounded: "{rounded.none}"
    padding: "0.45rem 0.65rem"
  button-control:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0.4rem 0.9rem"
  button-control-hover:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  button-control-pressed:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.none}"
  button-submit:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.none}"
    padding: "0.6rem"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0.4rem 0.6rem"
---

# Design System: Autonomous Code Review Engine — Dashboard

## Overview

**Creative North Star: "The One-Bit Windowed Console"**

This is a small desktop, not a scrolling report. Every persistent surface — Queue, each specialist, Activity.log, each Environment section — is an independent titled window with a solid inverted title bar, joined to the desktop's dithered ground rather than floating on a plain page. The palette is near-monochrome ink-on-paper with exactly one accent reserved for alert/active state; the type voice pairs a self-hosted one-bit pixel display face on titles only against a plain system sans everywhere else a solo operator actually has to read fast. Nothing here is decorative: the dither is a real background texture (not an unused token), the dashed "traceable thread" connector lights up only when a real pipeline relationship is active, and the single amber signal never appears except to mean "alert" or "running."

The system was built to replace a flat neutral-gray/blue-accent "Instrument Panel" world (the previous DESIGN.md) — that world is gone; nothing here should be read as a refinement of it. A hard offset drop-shadow was tried for `dialog` elevation during the build and explicitly rejected at finish review: it broke the one-bit material's own commitment to no soft/blurred or ambient depth, so `box-shadow: none` was restored everywhere, including on dialogs and popups.

**Key Characteristics:**
- Independent window chrome (inverted title bar + hairline border) replaces cards everywhere.
- Exactly one accent color (amber/signal), used only for alert or active state, never decoratively.
- Ordered dither stands in for a secondary/recessed surface, used as real background texture (desktop ground, chip/grid fills), not a swatch that goes unused.
- A self-hosted pixel display face is reserved for titles/headings only; body and controls stay in the system sans for scanability.
- Fully flat: no shadow anywhere, hairline borders carry all separation and depth.
- Plain `key : value` rows replace stat-tile grids as the default way to present a fact.

## Colors

Near-monochrome ink-on-paper, inverted wholesale for dark mode (not just re-tinted), plus exactly one warm accent.

### Primary
- **Signal Amber** (`#a3550a`, dark-mode `#d98a34`): the system's only accent. Used exclusively for alert/active state — a `.win-live` dot gone live, the `.thread` connector while a review is running, a failed specialist's status text, the critical-severity finding, focus outlines. Never used for a default/idle/decorative purpose.

### Neutral
- **Ink** (`#16150f`, dark-mode `#ece9dd`): primary text color and the fill of every inverted title bar; also every hairline border (`--line`).
- **Paper** (`#f1efe6`, dark-mode `#16150f`): the page background/desktop ground, and the text color sitting on an inverted (ink-filled) title bar.
- **Surface** (`#faf9f2`, dark-mode `#201f18`): the body fill of every window, popup, dialog, input, and env-section — the "page" a window sits on top of.
- **Muted** (`#5c5a4d`, dark-mode `#a6a293`): secondary/label text (`.kv-label`, field hints, table labels) and the idle-state dashed thread connector.
- **Dither Dot** (`rgba(22,21,15,0.55)`, dark-mode `rgba(236,233,221,0.4)`) / **Dither Dot Soft** (`rgba(22,21,15,0.22)`, dark-mode `rgba(236,233,221,0.18)`): the two densities of the ordered-dither radial-gradient texture — full-strength behind the whole page (7px grid), soft behind recessed fills like `.tile-chip` and `.provider-model-grid` (6px grid).

### Named Rules
**The One Warm Signal Rule.** The amber accent appears only in response to a real state change (something is running, something failed, something needs attention). It is never applied to a default, idle, or purely decorative element — an idle dot, an idle thread, and an OK status all render in ink or muted, never amber.

**The Full Inversion Rule.** Dark mode is not a re-tint; `--ink` and `--paper` swap roles wholesale (and every other token derives from that swap), so window title bars, which are always "ink-filled, paper-text," read correctly as inverted chrome in both themes without special-casing.

## Typography

**Display Font:** "Press Start 2P" (self-hosted, `/static/fonts/press-start-2p-v16-latin-regular.woff2`, served from this app's own static mount — never a Google Fonts CDN link, so a network hiccup can't silently revert the display voice on a page whose job is "tell at a glance whether the bot is healthy"), falling back to the system sans stack.
**Body Font:** the system sans stack (`-apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`) — no webfont.

**Character:** a one-bit pixel typeface announces every window and section as console chrome, while the system sans keeps every value, label, and control instantly legible — the pairing exists so the operator never has to parse pixel-font body copy under time pressure.

### Hierarchy
- **Display** (400 weight, 0.6rem–1rem, 1.6–1.7 line-height, uppercase, 0.03em tracking, `--pixel-font`): the page `h1`, every `.win-title`, every `.env-section h2`, and `h1.login-title` — nothing else. Deliberately tiny in pixels-per-em terms; the pixel face reads as chrome/label, not display-scale prose.
- **Body** (400 weight, ~0.85–0.95rem, system sans): stat values, table cells, form inputs, review rows.
- **Label** (400 weight, ~0.8–0.85rem, muted color, system sans): `.kv-label`, field hints/labels, table headers, timestamps.

### Named Rules
**The Titles-Only Pixel Rule.** The self-hosted pixel display face is applied only to `h1`, `.win-title`, `.env-section h2`, and `h1.login-title` — never to body text, buttons, nav labels, or table content. This is a deliberate Operate-mode scanability choice, not an oversight: a page whose entire job is "read state fast" cannot afford pixel-font body copy.

**The Graceful Hebrew Degradation Rule.** Press Start 2P has no Hebrew glyphs. Its font stack's fallback chain lets Hebrew text degrade per-character to the system sans automatically under `dir="rtl"` — this is intentional and correct, not a missing glyph to chase down or "fix" with a second display face.

## Layout

Single-column `main` capped at `max-width: 1100px` (`900px` for the Environment panel's sections), centered, with windows given a small asymmetric outer margin (`0 0.25rem 1.5rem 0`) so borders don't visually collide edge-to-edge. The Status panel is a vertical stack of windows: Queue window → traceable thread connector → a `.specialists-row` of three equal-flex specialist windows (`flex: 1 1 220px`) → Activity.log window. Below `640px`, the specialist row and every table collapse to a single-column stacked-card layout (`#renderVarsTable` drops its fixed columns and repeats each row as a bordered, self-labeled block; `.review-row` stacks its fields vertically with inline labels). `#configForm` is a two-column `label`/`field` grid (`max-content 1fr`) above `640px`, collapsing to one column (label stacked above its field) below it, so a long label like "Usage cap reset (UTC)" can't squeeze every input into a sliver. Spacing is a loose rem-based rhythm (0.3rem / 0.5rem / 0.85rem / 1.25rem / 1.5rem) rather than a strict numeric token scale.

## Elevation & Depth

Flat by commitment: **no `box-shadow` anywhere in the built system** — not on windows, popups, dialogs, or tooltips. This was a build-time decision under active pressure: a hard offset shadow was tried for dialog elevation and explicitly rejected at finish review as an unearned, craft-floor-refused device that contradicted the one-bit material's own "no shadow" world (a hard-edged shadow is still a lighting metaphor this flat material doesn't use). Depth is conveyed entirely through hairline borders (`1px solid var(--line)`) and solid fill contrast (an inverted title bar reads as "in front of" its body without needing a shadow to say so).

### Named Rules
**The No-Shadow, No-Exceptions Rule.** Every elevated surface — window, popup, dialog, tooltip, modal backdrop — uses `box-shadow: none` and a hairline border instead. A shadow value written into this codebase (there are two commented-out mentions describing a shadow that was tried and reverted) is a record of a rejected direction, not a live token; don't reintroduce one for a new surface.

## Shapes

Square everywhere: `border-radius: 0` is set explicitly on every bordered element (windows, inputs, buttons, popups, dialogs, table row-cards) — there is no rounded-corner token in this system at all. Borders are uniform 1px hairlines in `--line`, solid by default; `.kv-row` and `.finding` use a 1px **dashed** border-bottom instead, reserving the dash specifically for "a list of discrete facts" separators and for the traceable-thread connector (`2px dashed var(--muted)`, turning solid-colored amber when live) — dashing is never just decorative variation on a solid rule.

## Components

### Buttons
- **Shape:** square, no radius (`border-radius: 0`), 1px hairline border.
- **Control** (`.control`, `.nav-item`): surface-filled, ink text, hairline border; hover shifts the border to `--line` (not the accent) and, for `.control` specifically inside the dashboard shell, also swaps background to `--paper` — a "pressing inverts" idiom rather than a colored hover ring.
- **Active/pressed nav item** (`.nav-item.active`): fully inverted — ink background, paper text, ink border — the same inversion idiom as a window's own title bar.
- **Submit** (`.submit-btn`, login only): fully inverted at rest (ink background, paper text) since it's the one filled control on that page; hover shifts both background and border to `--muted`. It is deliberately never amber — there is nothing to alert on on the login page.
- **Focus:** every control gets `outline: 2px solid var(--accent)` with `1px` offset — the one place amber appears on a non-alert, non-active element, since focus-visible is itself a real state needing the system's one signal color.
- **Disabled:** `opacity: 0.5`, cursor `not-allowed`.

### Windows (signature component)
The system's core primitive, replacing cards everywhere. `.win` is a surface-filled, hairline-bordered box with no radius and no shadow. `.win-title` is its always-present inverted title bar (ink fill, paper text, pixel display font, uppercase) holding a label and, where relevant, a `.win-live` state dot (paper-colored and dimmed at rest, `opacity: 0.5`; snaps to full-opacity signal-amber with a brief scale-pulse animation when the state it tracks goes live). `.env-section` is the same pattern applied to a plain content section: its `h2` bleeds edge-to-edge as an inverted title bar identical in typography to `.win-title`. `.spec-win` is a `.win` sized to sit in a flex row of three (`.specialists-row`), one per LLM specialist.

### The Traceable Thread (signature component)
`.thread` is a 2px dashed vertical connector (`border-inline-start`) that visually joins the Queue window directly to the specialists row beneath it, using negative margin to consume the exact gap between the two windows' borders rather than floating short of either. It renders in muted gray at rest and switches to solid signal-amber (`.thread.is-live`) only while `queue.by_status.running` is actually true — a real signal read off live data, not a decorative flourish. This is the build's one memorable, load-bearing motion moment: a 0.6s scale-pulse keyframe on the accompanying `.win-live` dot the instant it goes live.

### Key:Value Rows
`.kv-row` is a flexed, space-between row (muted `.kv-label` at the start, bold `.kv-value` at the end) with a dashed bottom border, the last row in a group losing its border. This is the system's plain-fact primitive and it explicitly replaced a hero-metric, big-number-on-a-card stat-tile grid earlier in this build (see Do's and Don'ts) — every stat, every specialist's status, every table-row's mobile-collapsed fallback reduces to `.kv-row`, never to a standalone number-in-a-box.

### Chips
`.tile-chip` / `.tile-chip-list`: a hairline-bordered, soft-dithered-fill inline tag for queue-status counts and provider-backoff entries — no radius, small padding, `white-space: nowrap`.

### Cards / Containers
- **Corner style:** none (0 radius) throughout.
- **Background:** `--surface` for the window/section body; the dashboard's `.review-card` (an expandable review row + its findings) uses the same surface/hairline pattern as a window but without title-bar chrome, since it's a repeating list item rather than a persistent panel.
- **Shadow strategy:** none — see Elevation & Depth.
- **Border:** 1px hairline throughout; `.review-findings` adds a 1px top hairline to separate the summary row from its expanded detail.

### Inputs / Fields
- **Style:** surface fill, ink text, 1px hairline border, 0 radius, monospace for raw env values.
- **Focus:** 2px solid amber outline, 1px offset — the same focus treatment as buttons.
- **Disabled/readonly:** `opacity: 0.7`–`0.5`.
- **Password reveal:** an inline icon-button sitting inside the field (`.password-toggle`), positioned with logical properties (`inset-inline-end`) so it sits on the correct side automatically under Hebrew RTL without special-casing.

### Navigation
`.side-nav` is a row of `.control.nav-item` buttons (Status / Environment), each carrying an authored stroke-SVG icon (viewBox `0 0 24 24`, `stroke="currentColor"`, `stroke-width="2"`, round linecaps/joins) ahead of its label. The active panel's nav item is fully inverted (see Buttons above). The same icon convention drives the topbar's theme/language toggle buttons (sun/moon/monitor/globe) and the Environment tab's info-icon tooltips — one authored SVG vocabulary system-wide, no exceptions.

## Do's and Don'ts

### Do:
- **Do** give every persistent panel real window chrome (`.win`/`.win-title` or `.env-section`/`h2`) — an inverted title bar and a hairline border — never a plain card.
- **Do** reserve the amber signal color exclusively for alert/active state (a live dot, a running thread, a failed status, a critical finding, a focus ring). If a new element needs color and it isn't alerting or active, use ink or muted, never amber.
- **Do** apply the self-hosted pixel display face only to `h1`/`.win-title`/`.env-section h2`/`h1.login-title`. Every other text — buttons, nav, body, table cells — stays in the system sans.
- **Do** keep every corner square (`border-radius: 0`) and every shadow absent (`box-shadow: none`) on every new component; hairline borders are this system's only separation device.
- **Do** author new icons as inline SVG matching the existing stroke convention (`viewBox 0 0 24 24`, `stroke="currentColor"`, `stroke-width="2"`, round caps/joins) — never an icon font or a raster image.
- **Do** use `.kv-row` (plain label/value pairs) as the default way to present any fact or stat, including on new panels.
- **Do** use logical CSS properties (`inset-inline-end`, `border-inline-start`, `padding-inline-end`) for anything positioned relative to text direction, so it flips correctly under Hebrew RTL without a separate RTL stylesheet.

### Don't:
- **Don't** use a hero-metric/stat-tile-grid pattern (a big number alone in its own bordered box) for stats or summaries — this build replaced that exact pattern with `.kv-row` specifically because nested/stacked cards for individual numbers read as unearned decoration on this surface; treat the stat-tile grid as a retired anti-pattern, not an available alternative to `.kv-row`.
- **Don't** add a box-shadow to any element, including a "just this once" dialog/popup elevation need — a hard offset shadow was tried for exactly that during this build's finish review and rejected as contradicting the one-bit material's own no-shadow commitment. This is a hard invariant of this world, not a case-by-case judgment call.
- **Don't** use emoji as icons anywhere in this system — every icon is an authored inline SVG in the stroke convention described above.
- **Don't** load the display font from a third-party CDN (e.g. Google Fonts) — it is self-hosted from this app's own `/static/fonts` mount specifically so an unreachable third-party request can't silently revert the display voice on a health-status page.
- **Don't** invent a persistent sidebar navigation for today's two-panel scope. A sidebar is a confirmed acceptable future scaling path once more panels exist, but the current build's pill-tab `.side-nav` row is the deliberate choice for two panels — don't promote the future option into a present rule.
