---
name: Autonomous Code Review Engine — Dashboard
description: A flat, restrained instrument panel for a solo operator watching one bot's health.
colors:
  bg: "#f5f6f8"
  surface: "#ffffff"
  surface-2: "#eef0f3"
  text: "#1f2933"
  text-muted: "#5c6773"
  border: "#dde2e7"
  accent: "#3a6ea5"
  ok: "#2f7d4f"
  fail: "#b3454b"
  sev-critical: "#b3454b"
  sev-high: "#c07a2e"
  sev-medium: "#8a8330"
typography:
  body:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.4
  title:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
    fontSize: "1.3rem"
    fontWeight: 400
    lineHeight: 1.3
  label:
    fontFamily: "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 400
    lineHeight: 1.3
rounded:
  sm: "0.3rem"
  md: "0.4rem"
  lg: "0.6rem"
  xl: "0.75rem"
  pill: "999px"
spacing:
  xs: "0.3rem"
  sm: "0.5rem"
  md: "0.75rem"
  lg: "1rem"
  xl: "1.5rem"
components:
  button-control:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    padding: "0.4rem 0.9rem"
  button-control-hover:
    backgroundColor: "{colors.surface}"
  button-submit:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    rounded: "{rounded.md}"
    padding: "0.6rem"
    width: "100%"
  card-tile:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.lg}"
    padding: "0.85rem 1rem"
  card-popup:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.xl}"
    padding: "1rem 1.25rem"
  input-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "0.4rem 0.6rem"
---

# Design System: Autonomous Code Review Engine — Dashboard

## Overview

**Creative North Star: "The Instrument Panel"**

This is a gauge cluster, not a storefront. One person built and runs this
bot; this surface exists so that person can glance at it, know whether it's
healthy, and make one small adjustment, then look away. Every visual
decision optimizes for that: flat surfaces that don't compete for
attention, one signal color reserved for "this is active or actionable,"
and enough restraint that a table full of environment variables or a
findings feed doesn't turn into visual noise.

The system is calm, precise, and unadorned. It explicitly rejects the
SaaS-analytics-dashboard look — no gradients, no card shadows, no
decorative iconography, no marketing-site polish. Depth, when it appears at
all, marks something transient (a popup, a dialog) rather than dressing up
permanent chrome. The palette is almost entirely neutral gray-on-white (or
the dark-mode inverse); color is spent only where it means something:
status, severity, or "you can act here."

**Key Characteristics:**
- Flat-at-rest, borders instead of shadows on every persistent surface
- One accent color, spent sparingly and only on actionable/active elements
- Full light / dark / system theming via CSS custom properties, no
  light-only or dark-only assumptions baked into any component
- RTL-ready throughout (logical properties: `inset-inline-end`,
  `padding-inline-end`, not `left`/`right`)
- Severity and status communicated through color + text, never color alone

## Colors

The palette is almost entirely neutral; the one accent hue is the whole
color vocabulary's point of emphasis.

### Primary
- **Steady Signal Blue** (`#3a6ea5` light / `#7ba7d9` dark): The system's
  only "this is active, live, or actionable" signal. Used on the active nav
  tab, links, focus-visible outlines, and the login page's one submit
  button. Never used decoratively — every appearance of this color is load-
  bearing information.

### Neutral
- **Panel Gray** (`#f5f6f8` light / `#12161b` dark, token `bg`): Page
  background.
- **Card White** (`#ffffff` light / `#1a1f26` dark, token `surface`): Every
  card, tile, input, popup, and dialog background.
- **Recessed Gray** (`#eef0f3` light / `#22282f` dark, token `surface-2`):
  A second, slightly-sunken surface for content nested inside a card (chip
  backgrounds, the provider/model config sub-panel, hover state on icon
  buttons).
- **Ink** (`#1f2933` light / `#e6e9ec` dark, token `text`): Primary text.
- **Quiet Ink** (`#5c6773` light / `#9aa5b1` dark, token `text-muted`):
  Labels, hints, secondary metadata (timestamps, field labels).
- **Hairline** (`#dde2e7` light / `#2b323a` dark, token `border`): The only
  separator device in the system — every card, table, and input is defined
  by a 1px hairline border, never a shadow.

### Named Rules
**The One Signal Rule.** Steady Signal Blue appears only where it means
"active" or "actionable" — the current nav tab, a link, a focus ring, the
one primary submit action. It never decorates a heading, an icon at rest,
or a background. If a screen needs a second accent to feel finished, that's
a sign the layout needs restructuring, not a second color.

**The Status-Is-Never-Color-Alone Rule.** `ok`/`fail`/severity colors always
pair with text (a status word, a severity label) — never a bare colored dot
or bar as the only signal, since color alone fails both colorblind users and
theme edge cases.

## Typography

**Body Font:** -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
sans-serif (system stack; no webfont load, matching the "small and
dependency-light" product principle)

**Character:** A plain system-font stack, used with almost no hierarchy —
this system doesn't build a typographic voice so much as get out of the
way of the numbers and labels it's displaying.

### Hierarchy
- **Title** (400 weight, 1.3rem, 1.3 line-height): The one `<h1>` per panel
  ("Dashboard"). Not bold — weight is not how this system signals
  importance, position and color are.
- **Body** (400 weight, 1rem, 1.4 line-height): Default text size — stat
  values, table cells, form inputs.
- **Stat Value** (600 weight, 1.4rem): The one place real emphasis-by-
  weight appears — a stat tile's number, meant to be readable at a glance.
- **Label** (400 weight, 0.8rem–0.85rem, `text-muted` color): Stat labels,
  field hints, field labels, timestamps — small and muted rather than
  small-caps or letter-spaced.

### Named Rules
**The Weight-Is-Rare Rule.** Font-weight above 400 is reserved for exactly
two things: a stat tile's value, and a provider name in the config grid.
Everywhere else, hierarchy comes from size, color, and position — not bold
text.

## Layout

Single-column main content, `max-width: 1100px` (login: `360px`), centered.
No sidebar: the two-panel navigation (Status / Environment) is a horizontal
row of pill tabs directly under the page's one `<h1>`, not a persistent side
rail — this is a page with two views, not an app with many sections.

A fixed top-right utility bar (`header.topbar`) holds only account-level
controls (theme toggle, language toggle, logout) — separated from content
navigation by a hairline border, never mixed into the same row as the
panel tabs.

Density is comfortable, not compact: stat tiles run 4-up on desktop,
collapsing to 2-up at 900px and 1-up at 500px. Below 640px, the environment
variables table and review rows abandon their tabular/row layout entirely
and become stacked, self-labeled cards — a deliberate structural change
below that width, not just a font/padding squeeze.

## Elevation & Depth

Flat-by-default. Every persistent surface — stat tiles, review cards, the
environment-variables table, the config form — is separated from its
background by a 1px hairline border only, never a shadow. Depth is reserved
as a semantic signal for **transience**: it appears only on the theme/lang
popups and the two `<dialog>` elements, exactly the surfaces that will
disappear on the next click. A shadow appearing anywhere else would be a
mistake, not a stylistic variant.

### Shadow Vocabulary
- **Overlay** (`box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2–0.25)`): The one
  shadow value in the system, used identically on popups and dialogs.
- **Tooltip** (`box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2)`): A lighter
  variant for the small info-icon tooltip — still transient, slightly less
  prominent than a modal-level overlay.

### Named Rules
**The Shadow-Means-Temporary Rule.** If it has a shadow, it's going to
close. If it's meant to stay on screen, it gets a border instead.

## Shapes

Two form languages, split by permanence: pill shape (`border-radius: 999px`)
for every clickable control that lives in the topbar or nav row (`.control`,
`.nav-item`) — signaling "this is a button you press," and a soft
rectangular radius scale for everything that holds content:
`0.3rem`–`0.4rem` for small inline elements (chips, inputs, icon buttons),
`0.5rem`–`0.6rem` for cards and tiles, `0.75rem` for popups and dialogs —
the radius grows slightly with the surface's size. No sharp corners
anywhere; no corner exceeds `0.75rem` (nothing reaches for a fully
rounded "soft app" look either).

## Components

### Buttons
- **Control** (pill, `border-radius: 999px`): The default button —
  topbar icon buttons, panel-nav tabs, table row actions ("reveal",
  "delete", "retry"). Flat, `surface` background, `border` outline; on
  `:hover` only the border shifts to `accent` — no background change, no
  lift, no shadow. The active nav tab is the one variant that also colors
  its border and text with `accent`.
- **Submit** (rounded `0.4rem`, full-width): The single filled-accent
  button in the system, used only for the login form's submit action —
  intentionally the one place this system uses a solid accent fill,
  because it's the one screen where there's exactly one thing to do.
- **Hover / Focus:** Every interactive control shares one focus treatment:
  `outline: 2px solid var(--accent)` with `1px` offset — never a glow,
  never a background-color focus state.

### Chips (tile-chip)
- **Style:** `surface-2` background, `0.3rem` radius, small (`0.8rem`) text,
  no border — the one component that uses fill instead of outline, because
  it lives nested inside an already-bordered stat tile and a second border
  would be redundant.

### Cards / Containers
- **Corner Style:** `0.6rem` (stat tiles, review cards, the environment
  section panel).
- **Background:** `surface`.
- **Shadow Strategy:** None — see Elevation & Depth. Separation is the
  `border` hairline only.
- **Border:** `1px solid var(--border)` on every card, always.
- **Internal Padding:** `0.85rem 1rem` for tiles; `1rem 1.25rem 1.25rem`
  for the larger environment section panel.

### Inputs / Fields
- **Style:** `surface` background, `1px solid var(--border)`, `0.4rem`
  radius — visually identical to a button.control at rest, distinguished
  only by cursor and content.
- **Focus:** Same 2px accent outline as every other interactive element —
  inputs never get a special focus treatment of their own.
- **Error:** `fail`-colored helper text below the field (`.field-error`),
  never a red border on the input itself.
- **Readonly:** `opacity: 0.7` — the one place this system uses opacity
  rather than a color/border change to communicate state.

### Navigation
- **Panel tabs** (`.nav-item`): Pill buttons in a horizontal row directly
  under the page title, each with a small inline SVG icon
  (`stroke="currentColor"`, ~1.05rem) plus a label. The active tab is the
  only one carrying `accent` color/border; inactive tabs are visually
  identical to any other `.control` button.
- **Topbar:** Right-aligned row, separated from the page content by a
  hairline border, holding only theme/language/logout — never content
  navigation.

### Popups & Dialogs (signature component)
Two transient-content patterns, both using the system's one shadow value:
a positioned `.popup` (theme/language pickers, opened from a topbar button,
closed by a transparent full-screen backdrop) and a native `<dialog>`
(used elsewhere for confirmations). Both share the same visual shape
(`surface` background, `border`, `0.75rem` radius, overlay shadow) — the
`.popup` exists because a native `<dialog>` can't be positioned relative to
its trigger button the way these menus need to be.

## Do's and Don'ts

### Do:
- **Do** spend Steady Signal Blue only on the active/actionable element —
  never on a background, a heading, or a resting icon (The One Signal Rule).
- **Do** use a border, never a shadow, to separate any surface meant to
  stay on screen (The Shadow-Means-Temporary Rule).
- **Do** pair every status/severity color with a text label.
- **Do** keep every new component themeable through the existing CSS custom
  properties (`--bg`, `--surface`, `--text`, etc.) so light/dark/system
  theming stays automatic — never hardcode a hex value in a new rule.
- **Do** use logical properties (`inset-inline-end`, `padding-inline-start`)
  for anything positional, so RTL (Hebrew) keeps working without special-
  casing.

### Don't:
- **Don't** add a card shadow, gradient, or decorative icon anywhere — this
  system's whole identity is what it leaves out.
- **Don't** introduce a second accent color. If two things need to look
  distinct and important at once, that's a layout problem, not a palette
  gap.
- **Don't** give inputs a special focus treatment different from buttons —
  one focus ring style, applied uniformly, is a deliberate invariant.
- **Don't** build a persistent sidebar. Two views live in a pill-tab row
  under the title; adding more views means extending that row, not
  introducing a new navigation paradigm.
