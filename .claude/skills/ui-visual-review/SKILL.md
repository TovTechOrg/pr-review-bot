---
name: ui-visual-review
description: Screenshot a web UI change at light-desktop, dark-desktop, and mobile viewports using Playwright, to catch CSS/layout regressions invisible from reading source alone. Use before calling any change to dashboard/static/ or a static HTML page done.
---

# UI visual review

Reading HTML/CSS and reasoning about layout is **not a substitute** for
actually rendering the page. The 2026-09-03 Environment-tab CSS-grid
blowout (a `1fr` column forced to ~1700px by one unwrapped child, silently
stretching every other input sharing that column) and a mobile-only bug
where a table squeezed a value input to invisible near-zero width were
both invisible from source and only surfaced by rendering and measuring
the actual page.

## When to use

Before marking done any change that touches a page's markup, CSS, or
client-side JS layout logic (`dashboard/static/` in this project; the
equivalent static page in a sibling project). Skip only for changes with
no layout/visual surface at all (e.g. a pure backend/API change).

## How

1. **Start the relevant local server** if it isn't already running (see
   the project's own dev-server instructions).
2. **Run the helper script** shipped alongside this skill, with a **fresh
   `<out_dir>` every single capture pass** — e.g. `screenshots/attempt-1`,
   `screenshots/attempt-2`, never the same directory twice in one review:
   ```
   uv run --no-project python .claude/skills/ui-visual-review/screenshot_ui.py <url> <out_dir>
   ```
   This captures three PNGs into `<out_dir>`: `light-desktop.png`,
   `dark-desktop.png`, `mobile.png` (390×844, ~iPhone-width). If the page
   requires an authenticated session, pass a throwaway/test session cookie:
   `--cookie name=value` (repeatable). Never pass a real credential this
   way if a test-only one is available — this follows the same discipline
   as this project's own secret-handling rules.
3. **Read each PNG** (the `Read` tool renders images inline) and check for:
   - A grid/flex column or input forced far wider or narrower than its
     siblings (the grid-blowout shape).
   - An input, button, or table cell squeezed to near-zero width at the
     mobile viewport.
   - Text or interactive elements overlapping, clipped, or running off
     the visible area.
   - In dark mode: unreadable contrast, or an element that stayed on a
     light-mode color it shouldn't have.
   - If the page supports RTL languages: layout actually mirrors (not just
     that a `dir` attribute got set).
4. **Fix and re-run** until all three renders look correct. Don't declare
   the UI change done on the strength of the light-desktop screenshot alone.

## Re-reading discipline (don't burn tokens re-viewing what you've already seen)

Once you've read a PNG and written down what it showed, **don't `Read` that
exact file again later in the review just to double-check** — trust your own
earlier description instead of re-viewing an image whose content you already
have no reason to think changed.

This is *not* license to skip re-capturing after a fix. Every fix genuinely
needs a fresh screenshot and a fresh read, because the rendered page actually
changed — that's the point of step 4's loop. **The two are kept unambiguous
by always writing each capture pass to a brand-new `<out_dir>` (never
overwriting `light-desktop.png`/etc. in place).** A new path is unarguably a
new state that needs reading; an old path you've already read is unarguably
one you already described. Reusing the same filename across a fix→recapture
cycle collapses that distinction — a "have I already looked at this?" check
can no longer tell "yes, and nothing's changed since" apart from "yes, but
it's since been overwritten with a fix," which is exactly the mistake to
avoid in either direction.

## Script

`screenshot_ui.py` (this directory) needs nothing beyond this project's
existing `playwright` dependency — no MCP server, no extra install. See its
own docstring for the full CLI.
