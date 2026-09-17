# Demo Launcher Implementation Plan (Plan 3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An always-on launcher page on the existing GitHub Pages guide site that wakes the sleeping demo services, tells the reader honestly what is happening, and hands them off — plus a weekly health check that notices when the deployed demo breaks, a closing "deploy your own for real" call to action, and links to the demo from both repositories' READMEs.

**Architecture:** The launcher is a single static HTML file (`guide/demo/index.html`) copied verbatim into the mkdocs site and served at `/pr-review-bot/demo/`. It polls the target demo service's `/healthz` cross-origin, which requires each demo app to return an `Access-Control-Allow-Origin` header on that one path — added in each repo's `demo/app.py`, so the real services are untouched. The weekly check is a standalone Playwright script driven by an advisory-only scheduled workflow that, by construction, can never gate a push.

**Tech Stack:** Static HTML/CSS/vanilla JS (no build step, no dependencies), mkdocs-material passthrough, FastAPI middleware, Playwright (already a dev dependency), GitHub Actions, pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-live-mocked-demo-design.md` — read the "Hosting and instance-hours" section (the launcher's whole reason for existing) and "Follow-ups outside this design".

**Sibling plans, both shipped:** `docs/superpowers/plans/2026-09-16-demo-bot.md` (Plan 1, this repo) and `~/onboarding-wizard/docs/superpowers/plans/2026-09-17-demo-wizard.md` (Plan 2, sibling repo).

**This plan spans both repositories.** Each task is labelled with the repo it runs in. `~/onboarding-wizard` is a separate checkout with its own suite, its own CI, and its own `CLAUDE.md`; nothing here is visible from there.

---

## PREREQUISITE — land before executing this plan

**The dispatcher spams one WARNING per second on an idle queue, which buries this plan's analytics channel.** Found live on the deployed demo bot 2026-09-17:

```
WARNING:review_queue.dispatcher:skipping notice sweep: llm_request_timeout_seconds is not set; ...
```

Root cause (confirmed by reproduction, not by reading): `review_queue/dispatcher.py:324` returns `StepResult(action="idle")` when nothing is due, *before* reaching `_refresh_dispatcher_tuning_config()` at line 376 — the only non-test writer of the tuning cache (`main.py:139` merely validates at boot, never populates). `run_forever` calls `post_pending_notices` every iteration regardless, and that calls `require_config()` on the permanently empty cache. At `dispatcher_idle_sleep_seconds` = 1.0 (`config.py:133`) that is one line per second from boot until the first ticket is ever claimed.

**This is shared production code, not demo code** — the real `pr-review-engine` does it too. It is also a genuine functional gap, not just noise: the notice sweep's job is refreshing the "will retry at…" footnote on *deferred* tickets, and a queue whose only tickets are deferred is exactly an "idle" queue.

**Recommended fix:** give `post_pending_notices` its own throttled refresh, mirroring `run_forever`'s existing `_IDLE_SLEEP_REFRESH_INTERVAL_SECONDS` pattern for `dispatcher_idle_sleep_seconds` (whose docstring describes this same problem for the sibling value). Moving the refresh above the early return instead would issue one Supabase query per second forever.

It ships as its own change, with its own test, its own `code-review` pass, and an `ISSUES.md` incident entry. **Why it blocks this plan:** the spec's analytics are "structured log lines recording the furthest step reached, read from Render's logs during the launch window" — roughly 900 warning lines per 15-minute awake window makes every `demo_step` line unfindable exactly when it matters.

---

## Global Constraints

- **Secret handling overrides everything.** See `CLAUDE.md`'s first section. Nothing in this plan touches a secret: the demo dashboard credentials are `demo`/`demo`, baked into `Dockerfile.demo` and already public in the repository. **Do not add a GitHub Actions secret for the health check** — there is nothing to protect, and adding one creates a real credential where the design deliberately has none.
- **Measured values, not guesses.** Demo bot cold start measured 2026-09-17 at **41.6 s**, returning a held `HTTP/2 200` — Render holds the connection through boot rather than returning 502. Do not replace these with estimates.
- **Demo service URLs, confirmed live 2026-09-17** (Render only appends a random suffix when the slug collides, so these are the final hostnames):
  - Demo bot: `https://demo-pr-review-bot.onrender.com`
  - Demo wizard: `https://demo-onboarding-wizard.onrender.com`
  - Real wizard (CTA target): `https://onboarding-wizard-mk6m.onrender.com`
  - Guide site: `https://tovtechorg.github.io/pr-review-bot/`
- **No pinger may ever be attached to a demo service.** Confirmed 2026-09-17: no pingers exist on any service in the Render account. Render grants 750 free instance-hours per workspace per calendar month across four services; one continuously-warm service is ~730 h. A pinger on a demo service defeats this entire plan and risks suspending every free service in the workspace.
- **The launcher must never become an open redirect.** `?to=` selects between two hardcoded constants and accepts nothing else. No query parameter may ever reach `location.replace`/`assign`/`href`.
- **Mobile is the primary viewport** (spec: readers arrive overwhelmingly from a phone, much of it through LinkedIn's in-app browser). `ui-visual-review` gates Task 3 and Task 4 — the skill covers "a static HTML page", not just `dashboard/static/`.
- **Bilingual EN/HE with `dir` switching**, default English. Hebrew strings must not chain embedded LTR terms with an arrow; have a native reader check the Hebrew before merge.
- **Before any push:** `uv run pytest -v` and `uv run ruff check .` both green. Before any push to `main`: the `deploy-verify` skill. Neither substitutes for the other.
- **The advisory workflow is exempt** from "never push with a red suite", exactly as `.github/workflows/consumer-contract-lag.yml` is — see `CLAUDE.md`'s Conventions section.
- **`.claude/hooks/` files are shared byte-identical with the sibling repo.** This plan does not touch them. If something forces a change, port it in the same session and verify with `diff`.
- **Never modify** `.claude/hooks/check_env_access.py` or `redact_output.py`.

---

## File Structure

| Repo | File | Responsibility |
|---|---|---|
| bot | `guide/demo/index.html` | **Create.** The launcher. Self-contained: no external CSS, JS, fonts or images. |
| bot | `tests/test_launcher_page.py` | **Create.** Pins the two service URLs, the open-redirect guard, the timeout, mobile meta, noscript fallback. |
| bot | `demo/app.py` | **Modify.** `Access-Control-Allow-Origin` on `/healthz` only. |
| bot | `tests/test_demo_healthz_cors.py` | **Create.** Header present on `/healthz`, absent everywhere else. |
| bot | `demo/static/demo.js` | **Modify.** Closing CTA, real-wizard prewarm, `cta_clicked`. |
| bot | `scripts/demo_health_check.py` | **Create.** Standalone Playwright check. Never collected by pytest. |
| bot | `.github/workflows/demo-health.yml` | **Create.** Weekly, advisory-only. |
| bot | `tests/test_demo_health_workflow.py` | **Create.** Pins the advisory-only trigger shape. |
| bot | `README.md`, `guide/index.md` | **Modify.** Demo links. |
| wizard | `demo/app.py` | **Modify.** Same CORS header. |
| wizard | `demo/content.py` | **Modify.** Correct `DEMO_BOT_URL`'s wrong default. |
| wizard | `tests/test_demo_healthz_cors.py` | **Create.** Same two assertions. |
| wizard | `README.md` | **Modify.** Demo link. |

**Task order is load-bearing.** Tasks 1 and 2 must be deployed before Task 3's live verification can pass, because the launcher cannot read `/healthz` until the header exists on the running services.

---

### Task 1: Let the launcher read the demo bot's health (repo: `~/pr-review-bot`)

The launcher is served from `https://tovtechorg.github.io`, a different origin from `https://demo-pr-review-bot.onrender.com`. Verified live 2026-09-17: neither demo service currently returns any `access-control-allow-origin` header, so a browser `fetch` can reach `/healthz` but not read the result. Without this the launcher cannot distinguish "awake" from "still booting" and would redirect readers into a cold start — the exact thing it exists to prevent.

Scoped to `/healthz` alone, with no `Access-Control-Allow-Credentials`, so no cookie ever rides on the request and nothing else in the demo becomes readable cross-origin.

**Files:**
- Modify: `demo/app.py`
- Modify: `tests/test_demo_dashboard.py` (the `demo_chrome_installed` fixture)
- Test: `tests/test_demo_healthz_cors.py` (create)

**Interfaces:**
- Produces: `demo.app.LAUNCHER_ORIGIN` (`str`), consumed by Task 3's test as the expected value.

- [ ] **Step 1: Write the failing test**

Create `tests/test_demo_healthz_cors.py`:

```python
"""The launcher lives on GitHub Pages and must be able to READ /healthz
cross-origin to know when the demo has finished waking. Scoped to that one
path: nothing else in the demo is readable from another origin."""

from __future__ import annotations

import httpx
import pytest

import main as _main  # noqa: F401  (see tests/test_demo_dashboard.py for why)
import github_app as _real_github_app
import render_client as _real_render_client
import specialists.base as _real_specialists_base
from review_queue import store as _real_store

LAUNCHER_ORIGIN = "https://tovtechorg.github.io"


@pytest.fixture(autouse=True)
def _undo_global_rebinding(monkeypatch):
    """Identical to tests/test_demo_dashboard.py's fixture of the same name --
    importing demo.app runs install_mocks() at import time and permanently
    mutates the real, shared module objects via plain setattr."""
    for module in (_real_github_app, _real_store, _real_render_client):
        for name in dir(module):
            if not name.startswith("_"):
                monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(
        _real_specialists_base, "get_provider", _real_specialists_base.get_provider
    )


async def test_healthz_is_readable_from_the_launcher_origin(demo_env, demo_chrome_installed):
    transport = httpx.ASGITransport(app=demo_chrome_installed)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LAUNCHER_ORIGIN


async def test_no_other_route_becomes_readable_cross_origin(demo_env, demo_chrome_installed):
    """The header is scoped to one path, not applied app-wide.

    Deliberately asserts nothing about the status code: what matters is that
    no non-/healthz response carries the header, and that holds whether the
    route answers 200, redirects to /login, or 404s. Pinning a status here
    would couple this test to the demo's auth behaviour, which it is not
    about.
    """
    transport = httpx.ASGITransport(app=demo_chrome_installed)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/login")
    assert "access-control-allow-origin" not in response.headers
```

> **Do not add a test asserting the real `main.app` has no header.** `demo/app.py` mutates the shared `main.app` singleton irreversibly at import time; a test that reaches for the pristine app in the same worker corrupts the rest of the suite. The path check above is the honest, non-destructive version of that assertion.

Both fixtures (`demo_env`, `demo_chrome_installed`) are copy-pasted per this repo's existing convention — see `ISSUES.md`'s parked entry about the duplication. Copy them verbatim from `tests/test_demo_dashboard.py` into this new file.

- [ ] **Step 2: Run the test and confirm it fails**

```bash
uv run pytest tests/test_demo_healthz_cors.py -v
```

Expected: `test_healthz_is_readable_from_the_launcher_origin` FAILS with `KeyError: 'access-control-allow-origin'`. The second test should already PASS.

- [ ] **Step 3: Add the middleware**

In `demo/app.py`, immediately after the existing `_demo_request_context` middleware (which ends with `return response`, before `async def sweep_once`), add:

```python
# The launcher page (guide/demo/index.html, served from GitHub Pages) polls
# this service's /healthz to know when a cold start has finished. That is a
# cross-origin read, so the response needs an explicit allow header or the
# browser hands the launcher an opaque failure indistinguishable from "still
# booting" -- see the 2026-09-16 design's "The launcher" section.
#
# Scoped to /healthz by path, with no Access-Control-Allow-Credentials, so no
# cookie ever rides on it and no other demo route becomes readable from
# another origin. No `Vary: Origin`: the value is a constant that does not
# depend on the request's own Origin, so there is nothing for a cache to vary
# on.
LAUNCHER_ORIGIN = "https://tovtechorg.github.io"


@app.middleware("http")
async def _allow_launcher_health_polling(request, call_next):
    response = await call_next(request)
    if request.url.path == "/healthz":
        response.headers["access-control-allow-origin"] = LAUNCHER_ORIGIN
    return response
```

- [ ] **Step 4: Re-register it in the per-test fixture**

`conftest.py`'s restore fixture strips demo middleware off `main.app` after every test, so the new middleware needs the same per-test re-registration the others get. In `tests/test_demo_dashboard.py`'s `demo_chrome_installed` fixture, alongside the existing `app.add_middleware(BaseHTTPMiddleware, dispatch=_demo_request_context)` line:

```python
    from demo.app import _allow_launcher_health_polling
    app.add_middleware(BaseHTTPMiddleware, dispatch=_allow_launcher_health_polling)
```

Update the import line at the top of that fixture to `from demo.app import _allow_launcher_health_polling, _demo_request_context, app`. Copy the whole updated fixture into `tests/test_demo_healthz_cors.py` too.

- [ ] **Step 5: Run the tests and confirm they pass**

```bash
uv run pytest tests/test_demo_healthz_cors.py tests/test_demo_dashboard.py -v
```

Expected: all PASS.

- [ ] **Step 6: Run the full suite and lint**

```bash
uv run pytest -v && uv run ruff check .
```

Expected: both green. The middleware is registered on the shared `main.app` at demo-import time, so a regression here shows up as failures in unrelated dashboard tests — that is what this step is checking for.

- [ ] **Step 7: Commit**

```bash
git add demo/app.py tests/test_demo_healthz_cors.py tests/test_demo_dashboard.py
git commit -m "feat(demo): let the launcher read /healthz cross-origin

The launcher on GitHub Pages must distinguish 'awake' from 'still booting'.
Scoped to /healthz, no credentials, real service untouched.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

---

### Task 2: Same header on the demo wizard, and fix its wrong bot URL (repo: `~/onboarding-wizard`)

Two independent defects in one file pair, both blocking the reader's path.

**The CORS gap** is identical to Task 1 — the launcher's default target *is* the demo wizard, so without this the launcher cannot tell when its primary destination is ready.

**The wrong URL** is live today: `demo/content.py` declares `DEMO_BOT_URL = "https://demo-pr-review-engine.onrender.com"`, but the deployed service is `demo-pr-review-**bot**`. A reader who completes all four wizard steps is handed off to a hostname that does not exist. Fixing the constant rather than setting a `DEMO_BOT_URL` env var on the service is deliberate: the spec's whole argument for baking demo values into the image is that "a demo deployment then has no env-var configuration to get wrong". The env var stays as an override.

**Files:**
- Modify: `demo/app.py`
- Modify: `demo/content.py`
- Test: `tests/test_demo_healthz_cors.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_demo_healthz_cors.py` in `~/onboarding-wizard`:

```python
"""The launcher lives on GitHub Pages and must be able to READ /healthz
cross-origin to know when this service has finished waking."""

from __future__ import annotations

import httpx

from demo import content

LAUNCHER_ORIGIN = "https://tovtechorg.github.io"


async def test_healthz_is_readable_from_the_launcher_origin(demo_env):
    from demo.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LAUNCHER_ORIGIN


async def test_no_other_route_becomes_readable_cross_origin(demo_env):
    from demo.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert "access-control-allow-origin" not in response.headers


def test_the_handoff_points_at_the_service_that_actually_exists():
    """Deployed 2026-09-17 as demo-pr-review-bot, not demo-pr-review-engine.
    A reader who finishes all four steps lands here."""
    assert content.DEMO_BOT_URL == "https://demo-pr-review-bot.onrender.com"
```

Copy the `demo_env` fixture verbatim from `tests/test_demo_routes.py`.

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
uv run pytest tests/test_demo_healthz_cors.py -v
```

Expected: the CORS test FAILS with `KeyError`, the URL test FAILS comparing `demo-pr-review-engine` against `demo-pr-review-bot`, the third PASSES.

- [ ] **Step 3: Fix the URL default**

In `demo/content.py`, replace the `DEMO_BOT_URL` assignment:

```python
# Where "Finish & Deploy" sends the reader: the already-deployed bot demo.
# The hostname is the deployed service's real slug (demo-pr-review-BOT, not
# -engine -- corrected 2026-09-17 after the service was created and the old
# value was found to point at a hostname that never existed). Baked in rather
# than configured per-deployment, per the design's "no env-var configuration
# to get wrong" argument; DEMO_BOT_URL remains available as an override.
DEMO_BOT_URL = os.environ.get(
    "DEMO_BOT_URL", "https://demo-pr-review-bot.onrender.com"
)
```

- [ ] **Step 4: Add the middleware**

In `demo/app.py`, after the `_cookieless_visitors_share_one_session` middleware definition and before `__all__`:

```python
# The launcher page (in the sibling repo, at
# https://tovtechorg.github.io/pr-review-bot/demo/) polls this service's
# /healthz to know when a cold start has finished. That is a cross-origin
# read, so the response needs an explicit allow header or the browser hands
# the launcher an opaque failure indistinguishable from "still booting".
#
# Scoped to /healthz by path, with no Access-Control-Allow-Credentials, so no
# session cookie ever rides on it and no other route becomes readable from
# another origin.
LAUNCHER_ORIGIN = "https://tovtechorg.github.io"


@app.middleware("http")
async def _allow_launcher_health_polling(request, call_next):
    response = await call_next(request)
    if request.url.path == "/healthz":
        response.headers["access-control-allow-origin"] = LAUNCHER_ORIGIN
    return response
```

> Registration order matters in this file. `_cookieless_visitors_share_one_session` documents at length that it must stay outermost. Registering this one *after* it makes this the new outermost layer — which is fine, because this middleware only reads `request.url.path` and only writes a response header on the way out; it never touches cookies, the request scope, or the session. Do not move it above `_inject_demo_script`.

- [ ] **Step 5: Run the tests and confirm they pass**

```bash
uv run pytest tests/test_demo_healthz_cors.py -v
```

Expected: all three PASS.

- [ ] **Step 6: Run the full suite and lint**

```bash
uv run pytest -v && uv run ruff check .
```

Expected: both green.

- [ ] **Step 7: Commit**

```bash
git add demo/app.py demo/content.py tests/test_demo_healthz_cors.py
git commit -m "fix(demo): correct the bot handoff URL and allow launcher health polling

DEMO_BOT_URL pointed at demo-pr-review-engine; the deployed service is
demo-pr-review-bot, so every completed wizard run handed the reader a dead
hostname. Also adds the launcher's cross-origin /healthz read.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

---

### Task 3: The launcher page (repo: `~/pr-review-bot`)

Verified 2026-09-17: mkdocs copies a raw `.html` file inside `docs_dir` byte-for-byte into the built site, `mkdocs build --strict` stays green, and `guide/demo/index.html` lands at `site/demo/index.html` — served at `https://tovtechorg.github.io/pr-review-bot/demo/`. No `mkdocs.yml` change, no nav entry, no new workflow: the existing `pages` job already deploys it on every push to `main`.

Design decisions and the evidence behind them:

- **One long-lived request, not a retry burst.** Render holds the connection through a cold start and returns a real `200` (measured: 41.6 s). A short per-attempt timeout would abort ~20 requests that were each going to succeed.
- **120 s ceiling** — 41.6 s is one sample and the bot image is the heavier of the two, so roughly 3× headroom.
- **"Open it anyway" is visible the whole time**, not only after a failure. A browser that blocks cross-origin requests outright will never succeed, and holding such a reader for two minutes before offering an escape is the dishonest version of this page.
- **One fire-and-forget `POST` to the bot's `/api/demo/step/launcher`** both records the analytics step (already whitelisted in `demo/routes.py`, currently dead code) and *is* the bot's wake-up call, so the bot is warm by the time the reader finishes the wizard. A `no-cors` POST with no body and no custom headers is a CORS "simple request" — no preflight, no server change.

**Files:**
- Create: `guide/demo/index.html`
- Test: `tests/test_launcher_page.py`

**Interfaces:**
- Consumes: `demo.app.LAUNCHER_ORIGIN` from Task 1 (as a value to match, not an import).
- Produces: the public URL `https://tovtechorg.github.io/pr-review-bot/demo/`, consumed by Task 6's README links and Task 5's health check.

- [ ] **Step 1: Write the failing test**

Create `tests/test_launcher_page.py`:

```python
"""guide/demo/index.html -- the always-on page the article links to.

Pinned rather than merely present: this file is the FIRST thing a stranger's
browser talks to, it is not exercised by any other test, and its two service
URLs cannot be verified by anything else in this repo.
"""

from __future__ import annotations

import re
from pathlib import Path

LAUNCHER = (
    Path(__file__).resolve().parent.parent / "guide" / "demo" / "index.html"
).read_text(encoding="utf-8")

DEMO_WIZARD_URL = "https://demo-onboarding-wizard.onrender.com"
DEMO_BOT_URL = "https://demo-pr-review-bot.onrender.com"


def test_both_demo_service_urls_are_pinned():
    """Render only suffixes a slug when it collides, so these are stable --
    but a typo here is invisible until a reader hits a dead hostname, which
    is exactly how the sibling repo's DEMO_BOT_URL shipped wrong."""
    assert f'var DEMO_WIZARD_URL = "{DEMO_WIZARD_URL}";' in LAUNCHER
    assert f'var DEMO_BOT_URL = "{DEMO_BOT_URL}";' in LAUNCHER


def test_the_redirect_target_is_never_read_from_the_query_string():
    """?to= selects between two hardcoded constants and nothing else. Feeding
    a query parameter into location.* would turn a page on the org's own
    github.io domain into an open redirect."""
    assert "location.replace(TARGET);" in LAUNCHER
    assert re.search(r"location\.(replace|assign|href)[^\n]*params\.get", LAUNCHER) is None


def test_the_timeout_is_declared_once_and_bounded():
    assert "var TOTAL_MS = 120000;" in LAUNCHER


def test_it_renders_on_a_phone():
    """Mobile is the primary viewport: the audience arrives from LinkedIn."""
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in LAUNCHER


def test_a_no_js_visitor_still_gets_working_links():
    assert "<noscript>" in LAUNCHER
    assert LAUNCHER.count(DEMO_WIZARD_URL) >= 2


def test_nothing_is_loaded_from_a_third_party():
    """Self-contained on purpose: the launcher's whole value is being up when
    the demo is not, so it must not depend on a CDN, a font host, or an
    analytics script."""
    assert "<script src=" not in LAUNCHER
    assert "<link rel=\"stylesheet\"" not in LAUNCHER
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
uv run pytest tests/test_launcher_page.py -v
```

Expected: collection error — `FileNotFoundError` on `guide/demo/index.html`.

- [ ] **Step 3: Create the launcher**

Create `guide/demo/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Starting the demo — PR Review Engine</title>
<meta name="description" content="Launching the live demo of the PR review engine and its setup wizard.">
<meta name="robots" content="noindex">
<style>
  :root {
    --bg: #f6f7fb; --card: #ffffff; --fg: #1a1a2e; --muted: #5a5f73;
    --accent: #5b7cd6; --track: #e3e7f2; --border: #dfe3ef;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12141c; --card: #1b1e29; --fg: #e7ecff; --muted: #a6adc4;
      --accent: #7b93e0; --track: #2a2f40; --border: #2e3346;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 16px; background: var(--bg); color: var(--fg);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .card {
    width: 100%; max-width: 30rem; background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 1.75rem 1.5rem; text-align: center;
  }
  h1 { margin: 0 0 .5rem; font-size: 1.35rem; line-height: 1.3; }
  p { margin: 0 0 1rem; color: var(--muted); font-size: .95rem; }
  .track { height: 6px; background: var(--track); border-radius: 3px; overflow: hidden; margin: 1.25rem 0; }
  .bar { height: 100%; width: 0; background: var(--accent); transition: width .5s linear; }
  a { color: var(--accent); }
  .primary {
    display: inline-block; margin-top: .25rem; padding: .7rem 1.1rem; border-radius: 9px;
    background: var(--accent); color: #fff; text-decoration: none; font-weight: 600;
  }
  .links { margin-top: 1.25rem; font-size: .875rem; }
  .links a { display: inline-block; margin: 0 .5rem; }
  .lang { margin-top: 1.5rem; font-size: .8rem; color: var(--muted); }
  #failure { display: none; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<main class="card">
  <div id="waiting">
    <h1 id="title">Starting the demo…</h1>
    <p id="status">Waking the demo service — this usually takes about 45 seconds.</p>
    <div class="track"><div class="bar" id="bar"></div></div>
    <p><a id="anyway" href="https://demo-onboarding-wizard.onrender.com">Open it anyway →</a></p>
  </div>

  <div id="failure">
    <h1 id="failTitle">The demo didn't wake up in time.</h1>
    <p id="failBody">It runs on free hosting, so this happens occasionally. You can open it directly, or read how it works.</p>
    <p><a class="primary" id="failLink" href="https://demo-onboarding-wizard.onrender.com">Open it anyway →</a></p>
  </div>

  <div class="links">
    <a id="repoLink" href="https://github.com/TovTechOrg/pr-review-bot">View the repository</a>
    <a id="guideLink" href="https://tovtechorg.github.io/pr-review-bot/setup/">Read the setup guide</a>
  </div>

  <p class="lang"><a href="?lang=he" id="langLink">עברית</a></p>

  <noscript>
    <p>This page needs JavaScript to wake the demo. Open a service directly:</p>
    <p><a href="https://demo-onboarding-wizard.onrender.com">Setup wizard demo</a></p>
    <p><a href="https://demo-pr-review-bot.onrender.com">Review engine demo</a></p>
    <p>The first request takes about a minute — free hosting sleeps when idle.</p>
  </noscript>
</main>

<script>
(function () {
  // The two demo services. HARDCODED, never read from the query string: a
  // page on the org's own github.io domain that redirected to a
  // caller-supplied URL would be an open redirect. ?to= picks between these
  // two literals and nothing else.
  var DEMO_WIZARD_URL = "https://demo-onboarding-wizard.onrender.com";
  var DEMO_BOT_URL = "https://demo-pr-review-bot.onrender.com";

  // Measured 2026-09-17: a real cold start took 41.6s and Render HELD the
  // connection, answering 200 rather than 502. So this is one long request,
  // not a burst of short ones -- a short per-attempt timeout would abort
  // requests that were each about to succeed. 120s is ~3x the measurement,
  // headroom for the heavier bot image and a bad day.
  var TOTAL_MS = 120000;
  var RETRY_MS = 3000;
  var EXPECTED_MS = 45000;

  var STRINGS = {
    en: {
      title: "Starting the demo…",
      starting: "Waking the demo service — this usually takes about 45 seconds.",
      waking: "Still waking up. Free hosting sleeps when idle.",
      slow: "This is taking longer than usual.",
      anyway: "Open it anyway →",
      failTitle: "The demo didn't wake up in time.",
      failBody: "It runs on free hosting, so this happens occasionally. You can open it directly, or read how it works.",
      repo: "View the repository",
      guide: "Read the setup guide",
      other: "עברית"
    },
    he: {
      title: "מפעיל את הדמו…",
      starting: "מעיר את שירות הדמו — זה לוקח בדרך כלל כ-45 שניות.",
      waking: "עדיין מתעורר. אירוח חינמי נכנס לשינה כשאין פעילות.",
      slow: "זה לוקח יותר זמן מהרגיל.",
      anyway: "פתחו בכל זאת",
      failTitle: "הדמו לא התעורר בזמן.",
      failBody: "השירות רץ על אירוח חינמי, אז זה קורה מדי פעם. אפשר לפתוח אותו ישירות, או לקרוא איך זה עובד.",
      repo: "מאגר הקוד",
      guide: "מדריך ההתקנה",
      other: "English"
    }
  };

  var params = new URLSearchParams(location.search);

  var lang = params.get("lang");
  if (lang !== "he" && lang !== "en") {
    lang = (navigator.language || "en").toLowerCase().indexOf("he") === 0 ? "he" : "en";
  }
  var T = STRINGS[lang];
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "he" ? "rtl" : "ltr";

  // The ONLY thing ?to= does: pick one of the two literals above.
  var TARGET = params.get("to") === "bot" ? DEMO_BOT_URL : DEMO_WIZARD_URL;

  function $(id) { return document.getElementById(id); }

  $("title").textContent = T.title;
  $("status").textContent = T.starting;
  $("anyway").textContent = T.anyway;
  $("anyway").href = TARGET;
  $("failTitle").textContent = T.failTitle;
  $("failBody").textContent = T.failBody;
  $("failLink").textContent = T.anyway;
  $("failLink").href = TARGET;
  $("repoLink").textContent = T.repo;
  $("guideLink").textContent = T.guide;
  $("langLink").textContent = T.other;
  $("langLink").href = "?" + (params.get("to") === "bot" ? "to=bot&" : "") +
    "lang=" + (lang === "he" ? "en" : "he");

  // Records the analytics step AND wakes the bot in one request, so the bot
  // is warm by the time a reader finishes the four wizard steps. no-cors +
  // POST with no body and no custom headers is a CORS "simple request": no
  // preflight, so this needs nothing on the server side.
  try {
    fetch(DEMO_BOT_URL + "/api/demo/step/launcher", {
      method: "POST", mode: "no-cors", cache: "no-store"
    });
  } catch (err) { /* analytics must never break the launch */ }

  // AbortSignal.timeout is not in older in-app WebViews, and LinkedIn's is
  // exactly the environment this page has to survive.
  function timeoutSignal(ms) {
    if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
      return AbortSignal.timeout(ms);
    }
    var controller = new AbortController();
    setTimeout(function () { controller.abort(); }, ms);
    return controller.signal;
  }

  var started = Date.now();
  var done = false;

  function elapsed() { return Date.now() - started; }

  function tick() {
    if (done) return;
    var seconds = Math.floor(elapsed() / 1000);
    $("status").textContent =
      seconds < 25 ? T.starting : (seconds < 60 ? T.waking : T.slow);
    var fraction = Math.min(elapsed() / EXPECTED_MS, 0.95);
    $("bar").style.width = (fraction * 100) + "%";
  }

  function succeed() {
    done = true;
    location.replace(TARGET);
  }

  function fail() {
    done = true;
    $("waiting").hidden = true;
    $("failure").style.display = "block";
  }

  function poll() {
    if (done) return;
    var remaining = TOTAL_MS - elapsed();
    if (remaining <= 0) { fail(); return; }
    fetch(TARGET + "/healthz", {
      mode: "cors", cache: "no-store", signal: timeoutSignal(remaining)
    }).then(function (response) {
      if (response.ok) { succeed(); return; }
      retry();
    }).catch(retry);
  }

  function retry() {
    if (done) return;
    if (elapsed() >= TOTAL_MS) { fail(); return; }
    setTimeout(poll, RETRY_MS);
  }

  setInterval(tick, 1000);
  tick();
  poll();
})();
</script>
</body>
</html>
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
uv run pytest tests/test_launcher_page.py -v
```

Expected: all six PASS.

- [ ] **Step 5: Confirm mkdocs still builds strictly and emits the page**

```bash
uv run mkdocs build --strict -d /tmp/launcher-check && ls /tmp/launcher-check/demo/index.html
```

Expected: exit 0, the file exists. A Material for MkDocs "MkDocs 2.0 upcoming changes" banner is normal pre-existing noise (see `ISSUES.md`), not a failure.

- [ ] **Step 6: Visual review**

Invoke the `ui-visual-review` skill against `guide/demo/index.html` at light-desktop, dark-desktop, and mobile (390px). Check specifically:

- No horizontal scroll at 320px.
- The Hebrew variant (`?lang=he`) renders RTL with the progress bar and links correctly mirrored.
- The failure state is legible — force it by temporarily setting `TOTAL_MS` to `1`, screenshot, then restore it. Do not commit the temporary value.

- [ ] **Step 7: Run the full suite and lint, then commit**

```bash
uv run pytest -v && uv run ruff check .
git add guide/demo/index.html tests/test_launcher_page.py
git commit -m "feat(demo): add the GitHub Pages launcher

Wakes a sleeping demo service, polls /healthz, redirects. Honest failure
state after 120s rather than spinning forever. Timings from a measured
41.6s cold start.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

- [ ] **Step 8: Live verification (only after Tasks 1–2 have deployed)**

This cannot pass until the CORS header is live on the running services — see Appendix A. Once it is:

```bash
curl -sS -H "Origin: https://tovtechorg.github.io" -D - -o /dev/null \
  https://demo-onboarding-wizard.onrender.com/healthz | grep -i access-control
```

Expected: `access-control-allow-origin: https://tovtechorg.github.io`. Then open the deployed launcher in a real browser against a genuinely cold service and confirm it redirects rather than timing out.

---

### Task 4: The closing call to action (repo: `~/pr-review-bot`)

The spec's reader's path ends with "a closing **'deploy your own for real'** call to action links to the real wizard". `demo/routes.py` already whitelists a `cta_clicked` analytics step; nothing calls it. This builds the control that does.

The real wizard is unpinned too, so it also cold-starts. A `no-cors` `/healthz` ping fired when the CTA *renders* — the first moment a click becomes plausible — gives it a head start without the waste of warming it from the launcher minutes earlier, when it would have spun back down before the reader ever got here.

**Files:**
- Modify: `demo/static/demo.js`
- Test: `tests/test_demo_dashboard.py` (extend)

**Interfaces:**
- Consumes: `POST /api/demo/step/{name}` with `name="cta_clicked"` (already exists, `demo/routes.py:53`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_demo_dashboard.py`:

```python
def test_the_demo_script_builds_a_cta_pointing_at_the_real_wizard():
    """The demo's closing move is sending a convinced reader to the real
    thing. The URL is pinned because nothing else in this repo verifies it."""
    script = (
        Path(__file__).resolve().parent.parent / "demo" / "static" / "demo.js"
    ).read_text(encoding="utf-8")
    assert 'var REAL_WIZARD_URL = "https://onboarding-wizard-mk6m.onrender.com";' in script
    assert "cta_clicked" in script


def test_the_cta_warms_the_real_wizard_when_it_renders():
    """Not from the launcher: that fires minutes earlier, and a free service
    spins back down after ~15 idle minutes, so the instance-hours would be
    spent on a service that is asleep again by the time anyone clicks."""
    script = (
        Path(__file__).resolve().parent.parent / "demo" / "static" / "demo.js"
    ).read_text(encoding="utf-8")
    assert 'REAL_WIZARD_URL + "/healthz"' in script
```

Add `from pathlib import Path` to the file's imports if it is not already present.

- [ ] **Step 2: Run the test and confirm it fails**

```bash
uv run pytest tests/test_demo_dashboard.py -k cta -v
```

Expected: both FAIL on the missing constant.

- [ ] **Step 3: Implement the CTA**

In `demo/static/demo.js`, add these functions inside the existing IIFE, before the `document.addEventListener("DOMContentLoaded", ...)` block at the bottom:

```js
  // Where a convinced reader goes next. The real wizard is unpinned too, so
  // it cold-starts -- warmed below when the CTA renders, which is the first
  // moment a click on it is plausible. Warming it from the launcher instead
  // would spend the instance-hours minutes too early, and a free service
  // spins back down after ~15 idle minutes.
  var REAL_WIZARD_URL = "https://onboarding-wizard-mk6m.onrender.com";
  var GUIDE_URL = "https://tovtechorg.github.io/pr-review-bot/setup/";

  var CTA = {
    en: {
      heading: "Like what you see?",
      body: "That review came from mock data. Point the real engine at your own repository — it takes about 30 minutes.",
      primary: "Deploy your own →",
      secondary: "Read the setup guide"
    },
    he: {
      heading: "אהבתם?",
      body: "הסקירה הזו הופקה מנתונים מדומים. אפשר לחבר את המנוע האמיתי למאגר שלכם — זה לוקח כחצי שעה.",
      primary: "התקינו אצלכם",
      secondary: "מדריך ההתקנה"
    }
  };

  function ctaStrings() {
    return CTA[document.documentElement.lang === "he" ? "he" : "en"];
  }

  function addClosingCta() {
    // The dashboard only, never the login page: the CTA is the payoff at the
    // end of the reader's path, not a distraction in front of the gate.
    var reviews = document.getElementById("reviews");
    if (!reviews || document.getElementById("demoCta")) return;

    var strings = ctaStrings();
    var section = document.createElement("section");
    section.id = "demoCta";
    section.style.cssText =
      "margin:1.5rem auto;max-width:48rem;padding:1.25rem;border-radius:12px;" +
      "border:1px solid var(--border);background:var(--card);text-align:center";

    var heading = document.createElement("h2");
    heading.textContent = strings.heading;
    heading.style.cssText = "margin:0 0 .5rem;font-size:1.15rem";

    var body = document.createElement("p");
    body.textContent = strings.body;
    body.style.cssText = "margin:0 0 1rem;color:var(--text-muted);font-size:.925rem";

    var primary = document.createElement("a");
    primary.id = "demoCtaPrimary";
    primary.href = REAL_WIZARD_URL;
    primary.textContent = strings.primary;
    primary.rel = "noopener";
    primary.style.cssText =
      "display:inline-block;padding:.7rem 1.15rem;border-radius:9px;background:var(--accent);" +
      "color:#fff;text-decoration:none;font-weight:600;min-height:44px;line-height:1.7";

    var secondary = document.createElement("a");
    secondary.href = GUIDE_URL;
    secondary.textContent = strings.secondary;
    secondary.rel = "noopener";
    secondary.style.cssText = "display:inline-block;margin-top:.85rem;font-size:.875rem";

    primary.addEventListener("click", function () {
      // Fire-and-forget: navigation must never wait on analytics.
      try { fetch("/api/demo/step/cta_clicked", { method: "POST" }); } catch (err) {}
    });

    section.appendChild(heading);
    section.appendChild(body);
    section.appendChild(primary);
    section.appendChild(document.createElement("br"));
    section.appendChild(secondary);
    reviews.parentNode.insertBefore(section, reviews.nextSibling);

    // Warm the real wizard now that a click is plausible. no-cors because we
    // never read the answer -- this is a wake-up, not a health check.
    try {
      fetch(REAL_WIZARD_URL + "/healthz", { mode: "no-cors", cache: "no-store" });
    } catch (err) {}
  }
```

Then, inside the existing `DOMContentLoaded` listener, after the existing `fetch("/api/demo/bootstrap"...)` line, add:

```js
    addClosingCta();
```

- [ ] **Step 4: Run the tests and confirm they pass**

```bash
uv run pytest tests/test_demo_dashboard.py -v
```

Expected: all PASS.

- [ ] **Step 5: Visual review**

Invoke `ui-visual-review` against the demo dashboard at light-desktop, dark-desktop, and mobile (390px). Check:

- The CTA sits below the reviews list and does not overlap the sticky demo banner.
- The primary button clears a 44px touch target on mobile — this repo has a parked issue about exactly that measurement being missed before.
- No horizontal scroll at 320px.
- The Hebrew variant renders RTL correctly.

- [ ] **Step 6: Run the full suite and lint, then commit**

```bash
uv run pytest -v && uv run ruff check .
git add demo/static/demo.js tests/test_demo_dashboard.py
git commit -m "feat(demo): add the closing 'deploy your own' call to action

Wires the already-whitelisted cta_clicked analytics step and warms the real
wizard at the moment a click on it becomes plausible.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

---

### Task 5: The weekly health check (repo: `~/pr-review-bot`)

Nothing else in this design notices a *deployed* demo breaking — CI only sees build time. Scope is deliberately the dashboard payoff screen rather than all four wizard steps: per the spec, "a cron coupled to every step's markup breaks constantly and gets ignored, which is worse than no check."

Two structural rules, both load-bearing:

- **The script lives in `scripts/`, not `tests/`.** `pyproject.toml`'s `testpaths = ["tests", "dashboard/tests"]` means pytest never collects it, so `uv run pytest -v` stays offline and CLAUDE.md's "run the full suite before pushing" rule keeps working.
- **The workflow triggers on `schedule` and `workflow_dispatch` only.** Because it never runs on a commit or PR it can never produce a check run against one, so it cannot be selected as a required status check — the property is structural, not a convention someone has to remember. Identical reasoning to `.github/workflows/consumer-contract-lag.yml`.

**Files:**
- Create: `scripts/demo_health_check.py`
- Create: `.github/workflows/demo-health.yml`
- Test: `tests/test_demo_health_workflow.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_demo_health_workflow.py`:

```python
"""The weekly demo health check is ADVISORY. A red run means the deployed
demo is broken -- worth a look, never a push blocker. That must be
structural, not a convention: see .github/workflows/consumer-contract-lag.yml
for the same argument at length."""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "demo-health.yml"
)


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_it_can_never_gate_a_push_or_a_pull_request():
    """PyYAML parses a bare `on:` key as the BOOLEAN True, not the string
    "on" -- verified directly against this repo's existing workflows. Reading
    it as "on" silently yields None and makes this assertion vacuous."""
    workflow = _workflow()
    assert True in workflow, "expected a bare `on:` key"
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}


def test_it_runs_weekly():
    schedule = _workflow()[True]["schedule"]
    assert len(schedule) == 1
    # Day-of-week field pinned: a daily cron would wake the demo 7x a month
    # for no extra signal, against a shared 750-instance-hour budget.
    assert schedule[0]["cron"].split()[4] == "1"


def test_it_needs_no_write_scope_and_no_secret():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in _WORKFLOW.read_text(encoding="utf-8")


def test_it_actually_runs_the_check_script():
    steps = _workflow()["jobs"]["demo-health"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.demo_health_check" in commands
    assert "playwright install" in commands
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
uv run pytest tests/test_demo_health_workflow.py -v
```

Expected: FAIL — `FileNotFoundError` on the missing workflow.

- [ ] **Step 3: Write the check script**

Create `scripts/demo_health_check.py`:

```python
"""Weekly liveness check for the deployed demo.

Deliberately scoped to the dashboard payoff screen -- the one screen whose
breakage makes the article's link worthless -- rather than all four wizard
steps. A cron coupled to every step's markup breaks constantly and gets
ignored, which is worse than no check (2026-09-16 design, "Testing and CI").

Lives in scripts/ and NOT in tests/ on purpose: pyproject.toml's
`testpaths = ["tests", "dashboard/tests"]` means pytest never collects this,
so `uv run pytest -v` stays offline and CLAUDE.md's run-the-suite-before-push
rule keeps working.

No credentials: the demo dashboard's username and password are `demo`/`demo`,
baked into Dockerfile.demo and already public in this repository. There is
nothing here to put in a GitHub secret, and adding one would create a real
credential where the design deliberately has none.

Exit codes mirror scripts/check_consumer_contract.py:
  0  the demo works
  1  the demo is broken (the thing this check exists to find)
  2  the check itself could not run
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BOT_URL = "https://demo-pr-review-bot.onrender.com"
LAUNCHER_URL = "https://tovtechorg.github.io/pr-review-bot/demo/"

# The canned security finding from demo/content.py's FINDINGS_BY_SCHEMA.
# dashboard.html's findingRows() renders finding.description into a
# `.finding` div, HTML-escaped; this substring contains nothing that escaping
# would alter.
EXPECTED_FINDING = "The API key is written to the log in plaintext"

# A measured cold start was 41.6s (2026-09-17). Generous ceiling: a weekly
# check that cries wolf on a slow boot is a check people learn to ignore.
WAKE_TIMEOUT_S = 180
# The dashboard fires /api/demo/bootstrap on load, which enqueues a ticket
# the dispatcher then has to claim and run.
REVIEW_TIMEOUT_MS = 90_000


def _report(summary_path: str | None, lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)


def wake(url: str) -> float:
    """Block until the service answers, returning seconds waited.

    Render holds the connection through a cold start rather than returning
    502 (measured), so this is one long request, not a poll loop.
    """
    started = time.monotonic()
    response = httpx.get(f"{url}/healthz", timeout=WAKE_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return time.monotonic() - started


def check_dashboard() -> list[str]:
    """Drive the payoff screen. Returns failure reasons; empty means healthy."""
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(BOT_URL, wait_until="domcontentloaded", timeout=WAKE_TIMEOUT_S * 1000)

            # demo/static/demo.js pre-fills these and marks them readOnly.
            # Playwright's fill() refuses a readonly input, so assert the
            # prefill (itself a real check -- a reader who has to type
            # credentials into a demo has already lost) and click submit.
            if page.locator("#passwordInput").count():
                page.wait_for_function(
                    "document.getElementById('usernameInput')"
                    " && document.getElementById('usernameInput').value === 'demo'",
                    timeout=15_000,
                )
                page.locator("#loginForm button[type=submit]").click()

            page.wait_for_selector(".review-card", timeout=REVIEW_TIMEOUT_MS)
            # Cards render collapsed: expandedPrs is empty on a first load, so
            # `.review-findings` is display:none until the row is clicked.
            page.locator(".review-row").first.click()
            page.wait_for_selector(".review-card.expanded .review-findings", timeout=15_000)

            findings = page.locator(".review-card.expanded .review-findings").first.inner_text()
            if EXPECTED_FINDING not in findings:
                problems.append(
                    f"the canned finding text is missing from the expanded review card; "
                    f"got {findings[:200]!r}"
                )
        except PlaywrightTimeoutError as exc:
            problems.append(f"timed out driving the dashboard: {exc}")
        finally:
            browser.close()
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", help="path to write a Markdown report to")
    args = parser.parse_args()

    lines = ["## Demo health check", ""]

    try:
        launcher = httpx.get(LAUNCHER_URL, timeout=30, follow_redirects=True)
        launcher.raise_for_status()
        lines.append(f"- Launcher page: OK ({launcher.status_code})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- Launcher page: **UNREACHABLE** ({type(exc).__name__})")
        _report(args.summary, lines + ["", "Verdict: **CANNOT CHECK**"])
        return 2

    try:
        waited = wake(BOT_URL)
        lines.append(f"- Demo bot woke in {waited:.1f}s")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- Demo bot: **DID NOT WAKE** ({type(exc).__name__})")
        _report(args.summary, lines + ["", "Verdict: **BROKEN**"])
        return 1

    problems = check_dashboard()
    if problems:
        lines.append("- Dashboard: **BROKEN**")
        lines.extend(f"  - {problem}" for problem in problems)
        _report(args.summary, lines + ["", "Verdict: **BROKEN**"])
        return 1

    lines.append("- Dashboard: review rendered with the expected finding")
    _report(args.summary, lines + ["", "Verdict: **HEALTHY**"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write the workflow**

Create `.github/workflows/demo-health.yml`:

```yaml
name: Demo health (advisory)

# ADVISORY ONLY -- this workflow must never gate a push, a pull request, or a
# deploy. The mechanism is the trigger list below: `schedule` and
# `workflow_dispatch`, and nothing else. Because it never runs on a commit or
# a PR it can never produce a check run against one, which in turn means it
# cannot be selected as a required status check in branch protection -- the
# property is structural, not a convention someone has to remember. Same
# argument as .github/workflows/consumer-contract-lag.yml.
#
# A RED RUN HERE MEANS THE DEPLOYED DEMO IS BROKEN, which CI cannot see (CI
# only ever checks build time). It is NOT covered by CLAUDE.md's "never push
# with a red suite" rule. Exit 2 -- not 1 -- means the check could not run at
# all, which is the case worth actual alarm.
#
# NO SECRETS, DELIBERATELY. The demo dashboard's credentials are demo/demo,
# baked into Dockerfile.demo and already public in this repository. Adding a
# secret here would create a real credential where the design has none.
on:
  schedule:
    # Weekly, Mondays. Daily would wake the demo 7x a month for no extra
    # signal, against a 750-instance-hour budget shared by four services;
    # monthly would let a break sit through most of an article's life.
    #
    # :41 rather than :00 because GitHub's scheduler is heavily oversubscribed
    # on the hour and delays runs queued there.
    #
    # GitHub disables scheduled workflows in a repository with no activity for
    # 60 days. If this silently stops running, check that first.
    - cron: "41 7 * * 1"
  workflow_dispatch:

# Least privilege: this job reads a public website and writes a step summary.
permissions:
  contents: read

concurrency:
  group: demo-health
  cancel-in-progress: true

jobs:
  demo-health:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - name: Install uv
        uses: astral-sh/setup-uv@v10.0.1

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --all-extras --dev

      - name: Install Chromium
        run: uv run playwright install --with-deps chromium

      # Waking the demo costs ~0.25 instance-hours (Render spins a free
      # service down after ~15 idle minutes), i.e. ~1.1 h/month against the
      # workspace's 750.
      - name: Check the deployed demo
        run: uv run python -m scripts.demo_health_check --summary "$GITHUB_STEP_SUMMARY"
```

- [ ] **Step 5: Run the tests and confirm they pass**

```bash
uv run pytest tests/test_demo_health_workflow.py -v
```

Expected: all five PASS.

- [ ] **Step 6: Run the check against the live demo**

```bash
uv run playwright install chromium
uv run python -m scripts.demo_health_check
```

Expected: `Verdict: **HEALTHY**`, exit 0. If the review card never appears, check the demo bot's Render logs for the dispatcher warning this plan's PREREQUISITE section describes — an unfixed tuning cache defers every ticket.

> This step is a real cold start plus a real review. Run it **once**. Do not loop it to debug — a demo failure is diagnosed from Render's logs, not by re-running the check.

- [ ] **Step 7: Run the full suite and lint, then commit**

```bash
uv run pytest -v && uv run ruff check .
git add scripts/demo_health_check.py .github/workflows/demo-health.yml tests/test_demo_health_workflow.py
git commit -m "feat(demo): add the weekly deployed-demo health check

Advisory-only by construction: schedule + workflow_dispatch triggers mean it
can never become a required status check. Drives the dashboard payoff screen
only, per the spec's argument against markup-coupled crons.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

---

### Task 6: Link the demo from both repositories (repos: both)

The launcher is worthless if nothing points at it. Both READMEs already carry a "Try it →" line, so these follow an established shape rather than inventing one.

**Files:**
- Modify: `~/pr-review-bot/README.md`
- Modify: `~/pr-review-bot/guide/index.md`
- Modify: `~/onboarding-wizard/README.md`

- [ ] **Step 1: Add the demo link to this repo's README**

In `~/pr-review-bot/README.md`, insert immediately **before** the existing `**[Deploy your own →](...)**` paragraph:

```markdown
**[See it work → live demo](https://tovtechorg.github.io/pr-review-bot/demo/?to=bot)**
— a pull request getting reviewed, end to end, on mock data. No signup, no
credentials, nothing real is called. Runs on free hosting, so the first load
takes about a minute to wake up.
```

- [ ] **Step 2: Add it to the guide's home page**

In `~/pr-review-bot/guide/index.md`, insert immediately after the opening paragraph (the one ending "piling up new ones.") and before the `**Needs:**` line:

```markdown
!!! tip "Try it first"

    **[Open the live demo →](demo/?to=bot)** — the engine reviewing a pull
    request on mock data, or **[start from the setup wizard →](demo/)** to see
    how a deployment gets provisioned. Free hosting, so the first load takes
    about a minute.
```

**Verified 2026-09-17, not assumed:** this exact relative link builds green under `--strict`. MkDocs logs `Doc file 'index.md' contains an unrecognized relative link 'demo/?to=bot', it was left as is` at **INFO**, not WARNING, so strict mode does not fail on it — and the built `site/index.html` carries `href="demo/?to=bot"` pointing at the real `site/demo/index.html`. The link is "unrecognized" only in the sense that it targets a static passthrough file rather than a Markdown page.

- [ ] **Step 3: Add it to the wizard's README**

In `~/onboarding-wizard/README.md`, insert immediately **before** the existing `**[Try it →](...)**` line:

```markdown
**[See it work → live demo](https://tovtechorg.github.io/pr-review-bot/demo/)**
— walk the wizard end to end against mocked Render, GitHub and LLM
integrations, then watch the bot it "provisions" review a pull request. No
credentials, nothing real is created.
```

- [ ] **Step 4: Verify the guide still builds strictly**

```bash
cd ~/pr-review-bot && uv run mkdocs build --strict -d /tmp/links-check
```

Expected: exit 0. `--strict` turns a broken internal link into a failure, which is the point of running it here.

- [ ] **Step 5: Run both suites and lint, then commit in each repo**

```bash
cd ~/pr-review-bot && uv run pytest -v && uv run ruff check .
git add README.md guide/index.md
git commit -m "docs: link the live demo from the README and the guide

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

```bash
cd ~/onboarding-wizard && uv run pytest -v && uv run ruff check .
git add README.md
git commit -m "docs: link the live demo from the README

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PywMSErJE69CuyGiP9ecww"
```

---

## Appendix A: Deployment and live verification (not a code task)

The two demo Render services already exist and answer (verified 2026-09-17). What remains is a sequencing checklist, not a build step.

- [ ] Confirm both services auto-deploy from `main`. If they do not, trigger a manual deploy of each **after Tasks 1 and 2 merge** — the launcher cannot read `/healthz` until the CORS header is live on the running services.
- [ ] Verify the header reached production:
  ```bash
  for u in https://demo-pr-review-bot.onrender.com https://demo-onboarding-wizard.onrender.com; do
    curl -sS -D - -o /dev/null -H "Origin: https://tovtechorg.github.io" "$u/healthz" | grep -i access-control
  done
  ```
  Expected: `access-control-allow-origin: https://tovtechorg.github.io` from both.
- [ ] Confirm `DEMO_BOT_URL` is **not** set as an env var on the demo wizard service. Task 2 bakes the correct value into the image; a stale env var would silently override it.
- [ ] Confirm no pinger/monitor is attached to either demo service, or to the real bot or real wizard.
- [ ] Let both demo services go idle for 20 minutes, then open the deployed launcher on a phone and confirm it redirects rather than timing out. This is the only test of the whole path that matters.
- [ ] Record the observed cold-start duration. If it lands materially above ~90 s, revisit `TOTAL_MS` and `EXPECTED_MS` in `guide/demo/index.html`.

## Appendix B: Measured facts and open assumptions

**Measured 2026-09-17, not estimated:**

| Fact | Value | How |
|---|---|---|
| Demo bot cold start | 41.6 s, held `HTTP/2 200` | `curl -w '%{time_total}'` against `/healthz` |
| Demo wizard warm response | 0.34 s | same |
| CORS header today | absent on both | same, response headers inspected |
| mkdocs passthrough | raw HTML copied verbatim, `--strict` green | temporary `guide/demo/index.html`, `mkdocs build --strict` |
| PyYAML parses `on:` | as boolean `True` | `yaml.safe_load` on `consumer-contract-lag.yml` |
| Render instance-hours | 468 / 750 used, both pingers paused | operator read of `dashboard.render.com/billing#included-usage` |

**Assumptions an implementer should not silently inherit:**

- **The 41.6 s cold start is a single sample.** `TOTAL_MS = 120000` carries ~3× headroom for that reason. Appendix A's last step is where it gets a second data point.
- **Hebrew copy in the launcher and the CTA has not been reviewed by a native speaker.** It is written to be correct and plain, but have it read before merge.
- **The launcher assumes Render keeps holding the connection through a cold start.** If Render ever starts returning 502 instead, the `.catch(retry)` path already handles it — the launcher degrades from one long request to a 3-second poll, which still works.

## Appendix C: Deliberately out of scope

- **Creating the Render services** — already done manually, outside any code change.
- **The wizard's parked "skip to the dashboard" link** (`~/onboarding-wizard/ISSUES.md`). The launcher's `?to=bot` now gives the article a skip-to-the-payoff entry point at a better altitude — before the reader has invested any clicks — which largely supersedes the in-wizard shortcut. Revisit only if reader behaviour suggests otherwise.
- **The wizard's dead `POST /api/demo/step/{name}` endpoint** (`~/onboarding-wizard/ISSUES.md`). This plan wires the *bot's* copy, which is the one the launcher and the CTA need. The wizard's own per-step analytics remain uncalled.
- **`cost.md`'s two stale claims** — the spec's own follow-up list already carries them, and neither blocks the demo.
