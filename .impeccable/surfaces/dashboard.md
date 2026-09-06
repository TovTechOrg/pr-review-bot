---
version: 1
slug: "dashboard"
primary_target: "dashboard"
related_targets: []
---

# Surface Brief: dashboard

## Scope and visitor mode

Whole dashboard (Status panel, Environment panel, login page) as one
coherent world. Operate mode: solo operator, glance-then-act.

## Audience, job, action, proof, constraints

- Audience: the one person who deployed this bot's own instance.
- Job: tell at a glance whether the bot is healthy; make a quick config
  change without redeploying.
- Action: read queue/review/specialist state; peek a masked env value;
  edit an env var or provider/model override.
- Proof/content: real data only, from `/api/dashboard` and
  `/api/environment/render` — no invented content.
- Constraints: plain HTML/CSS/JS, no framework/build step. Must preserve
  the masked-by-default/reveal-toggle secret pattern, the read-only vs.
  write-scoped boundary, English/Hebrew + RTL, light/dark/system theming.
  Mobile is first-class, not an afterthought. Empty/typical/dense states
  need mocked fixtures/test data to actually exercise, not just describe.

## Direction contract

THESIS: Independent titled windows replace the scrolling card page —
Queue, each specialist, Activity.log, and Env each get real window
chrome, not a card.
OWN-WORLD: Near-black ink on paper-white (inverts dark); ordered dither
stands in for secondary surfaces; one amber accent, only for
alerts/active state.
STORY: Operator reads window states at a glance, traces a pipeline via a
dashed connector, peeks a masked secret or edits a config value, closes
the tab.
FIRST VIEWPORT: Status panel as a small desktop — a Queue window, one
per specialist, an Activity.log window, joined by dashed threads where a
real pipeline relationship exists.
FORM: One-bit windowed desktop; challenger `medium-native-one-bit-desktop`;
seed `b8b1d930`; raised by the Ruling Engine (hairline borders), the
Traceable Thread (connectors), the One Warm Signal (single accent).
FINISH: unreviewed and undocumented is unfinished; this build ends with
the finish review, the verdict, DESIGN.md, and every shipping raster
carrying its provenance.

## Memorable moment

The dashed "traceable thread" connector lighting up between the Queue
window and the specialist window currently running a review — the one
moment that makes the pipeline visible, not just its endpoints.

## Unresolved decisions (a builder must not invent)

- Exact dither pattern implementation (CSS pattern fill vs. SVG texture).
- The amber accent's precise value.
- Whether windows get real desktop drag interaction or fixed-position
  chrome only.
- Navigation topology: a pill-tab row (today's two panels) vs. a
  persistent sidebar. Not prohibited — confirmed 2026-09-06 that a
  sidebar is an acceptable future scaling path once more panels exist;
  don't invent one for today's two-panel scope without a real need.

## Execution contract

Code-led for this build — confirmed 2026-09-06 after two live comp
attempts against FLUX.1-schnell (the only image-gen backend available)
both failed to hold this direction's precision (browser chrome/sidebar
leaked in despite explicit exclusion, the single amber accent and the
dashed connector didn't render reliably). No approved comp exists; the
ambition lives entirely in this file's Direction contract, audited at
the finish review against written FIRST VIEWPORT/FORM promises instead
of pixel-diffing a shaky render. `.impeccable/config.json` still records
`comp` as the project's standing default — this flip is scoped to this
surface only, not a project-wide change.
