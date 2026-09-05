# dashboard/ — module boundaries and contracts

Loaded when working with files under `dashboard/`. Project-wide conventions
live in the root `CLAUDE.md`.

## What this package is

The ops/demo dashboard: `GET /` (static page) + `GET /api/dashboard` (JSON),
plus the session-cookie login flow that gates both. Deployed in the same
process as the main review app at the repo root (one Render service, one
Dockerfile — see `Dockerfile`), not as its own service.

## Layering

- `dashboard/router.py` reads `review_queue.store`, `review_queue.dispatcher`, and
  `providers.base.KNOWN_PROVIDERS` directly, and stays read-only (never
  enqueues, never mutates provider state).
- `dashboard/environment.py` is the one place `dashboard` writes anything —
  Render env vars (via `render_client`) and `runtime_config` overrides
  (via `review_queue.store`'s existing `get_*`/`set_*` functions). Every value
  it returns from `GET /api/environment/render` is a real Render secret
  value, not reduced to a boolean/length — a documented, scoped exception to
  root `CLAUDE.md`'s "never display a byte of a secret" rule (see that
  file's Secret handling section and
  `docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md`).
  The value only ever reaches the authenticated operator's own browser DOM
  (masked by default, toggle-revealed client-side) — never logged, never
  persisted beyond the response.
- `dashboard/auth.py` reads only `config.settings` (the three
  `DASHBOARD_*` credential fields) — no queue/provider access.
- `main.py` mounts `dashboard.router.router` and `dashboard.auth.router`
  — the one place the root app depends on `dashboard`. Neither project
  declares the other in its own `pyproject.toml` `dependencies` (see the
  2026-08-29 project-restructure design spec, section A) — the root project
  is the workspace root and `dashboard` its one member, sharing one venv.

## Contracts

- Every dashboard/auth route assumes it runs behind `main.py`'s
  `SessionRequired` exception handler and `require_session` dependency —
  this package does not re-implement that wiring itself.
- `dashboard/static/dashboard.html` and `dashboard/static/login.html` are
  read once at import time (`_STATIC_DIR = Path(__file__).parent /
  "static"`) — moving either file requires keeping them siblings of
  `router.py`/`auth.py`.
