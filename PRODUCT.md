# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary user: a solo operator (the person running this bot's own deployment)
— the same person who deployed and configures it. They use the dashboard
to check on their running instance, not to serve reviewees or a team.

## Product Purpose

The repo is an autonomous PR-review engine: a GitHub webhook receiver that
runs three parallel LLM specialists (Security, Performance, Code Quality)
against an opened/updated pull request's diff and posts the merged findings
back as a single, editable PR comment. The `dashboard/` app is this engine's
ops surface — a small authenticated web UI for watching and steering that
running instance. Success for the dashboard is: the operator can tell at a
glance whether the bot is healthy (queue state, recent reviews, cost) and
can make a quick config change (provider/model override, an env var) without
redeploying.

## Positioning

Not a customer-facing product — it's operational tooling for the person who
owns a deployment of this specific bot. Its value is condensing the review
engine's internal state (queue, provider/model routing, cost, live env vars)
into one place a single operator checks and acts from directly, instead of
tailing logs or opening the Render console.

## Operating Context

- Deployed in the same process/Render service as the review engine itself
  (one Dockerfile), gated by a session-cookie login (`dashboard/auth.py`).
- Two known panels in the current implementation: a status panel (queue
  stats + recent reviews) and an environment panel (Render env vars, with
  per-row reveal-to-view since dashboard/environment.py is a documented
  exception to the "never display a secret" rule elsewhere in this repo).
- Read path (`dashboard/router.py`) is read-only against the review queue,
  dispatcher, and provider registry. Write path is scoped to
  `dashboard/environment.py`: Render env vars and DB-backed runtime-config
  overrides (e.g. provider/model override) — never the queue or provider
  clients directly.
- Values shown or changed here are real operational/secret state for a
  live deployment — see root `CLAUDE.md`'s Secret handling section before
  touching anything that displays credentials.

## Capabilities and Constraints

- Solo-operator scale: no multi-tenant accounts, no roles/permissions
  beyond "logged in or not" (three `DASHBOARD_*` credential fields).
- Backend is FastAPI + a static HTML/CSS/JS page pair
  (`dashboard/static/dashboard.html`, `login.html`), served at import time
  from `dashboard/static/` — no JS framework/build step currently in use.
- Cost is a real constraint on the whole project (target ≈ $8–10/mo prod,
  $0 demo on free tiers — see `cost.md`); this shapes hosting/build choices
  but is not itself a dashboard UI concern.

## Stack

Plain HTML/CSS/JS, no framework or build step — confirmed 2026-09-06 during
the dashboard redesign's shape interview, resolving the open question noted
in an earlier revision of this file. Reason: matches the "small and
dependency-light" product principle below; a solo-operator ops tool doesn't
warrant a framework runtime or build pipeline to maintain.

## Brand Commitments

None established. No name/logo/voice commitments beyond the repo's own
title ("Autonomous Code Review Engine — Project ד").

## Evidence on Hand

No screenshots, testimonials, or case studies on hand. The only real
"evidence" is the live running dashboard.html/login.html markup itself and
the actual data it renders from `/api/dashboard` — future design work should
treat that markup as the incumbent visual truth to inspect, not invent
against.

## Product Principles

- Operator-first: every design decision optimizes for the one person who
  deployed this bot checking health and making a fast config change — not
  for a wider audience or for demo polish as the primary goal.
- Read-only by default, narrow writes: the UI should keep making the
  router's read-only/write-scoped boundary legible rather than blurring it.
- Secrets are structurally different from other data on this surface — any
  redesign must preserve the masked-by-default/reveal-toggle pattern for
  the environment panel, not just visually restyle it.
- Mobile is a first-class target, not an afterthought — confirmed
  2026-09-06 during the dashboard redesign's shape interview. The operator
  may check the dashboard from a phone; layout and interaction must hold up
  there with real quality, not just avoid breaking.
- Small and dependency-light: this is ops tooling for one process, not a
  product to scale a design system for — avoid over-engineering the surface
  beyond what a solo operator's workflow needs.

## Accessibility & Inclusion

No product-specific requirement established beyond general web
accessibility good practice.
