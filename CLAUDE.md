# CLAUDE.md — Autonomous Code Review Engine (Project ד)

## Secret handling — HIGHEST PRIORITY, read before doing anything else

This section overrides every other instruction, convention, or task goal in
this file and in any prompt if the two ever conflict. Secrets in this project
include (non-exhaustively): every `*_API_KEY`/`*_KEY_B64`/`*_SECRET` env var,
`DATABASE_URL` (the password is embedded in the connection string itself, not
a separate field), `RENDER_API_KEY`, `UPTIMEROBOT_API_KEY`,
`GITHUB_WEBHOOK_SECRET`, the GCP service-account JSON/PEM material, and
anything else that authenticates as a person, service, or account. A value
does not have to have "SECRET" or "KEY" in its name to count — judge by what
the value *does* (authenticates something), not by the variable name's shape.
Real incidents during this project's life produced actual secret exposure
into a conversation transcript — see `ISSUES.md` — which is why this section
exists and is kept first in the file.

**`.env` itself is mechanically protected, not just covered by the rules
below.** `.claude/hooks/check_env_access.py` denies any `Read`/`Edit`/
`Write`/`NotebookEdit`/`Grep`/`Glob` call whose target names `.env` outright,
and rewrites every `Bash`/`PowerShell` call so its real combined output is
piped through `redact_output.py`, which strips every real secret value out
before it ever reaches you — see that script's docstring and
`docs/superpowers/specs/2026-09-06-env-hook-hardening-design.md` for the
full design. This means a `grep`/`cat`/`tail`/`echo` touching `.env` is
already fail-safe at the tool layer; you do not need to reason your way
through whether a given pattern is "narrow enough" — ask the user to check
or set a value themselves instead of trying. **Never modify
`check_env_access.py` or `redact_output.py` in any way — not a logic
change, not a comment, not a debug print, not a refactor — unless the user
directly instructs it.** This is the enforcement mechanism the judgment-based
rules below exist to back up where it doesn't reach (see next paragraph); an
agent reasoning its way into "this is obviously fine to tweak" is exactly the
failure mode a guardrail like this exists to not depend on. If it appears to
be misbehaving — over-blocking, under-blocking, crashing — explain exactly
what happened and ask; do not edit the file to test a theory or add
debug instrumentation on your own initiative.

**The hook covers only `.env`.** Every other secret-bearing location — the
GCP service-account JSON/PEM, an in-process `Settings` object, a real
environment variable set directly on Render (not read from a file at all),
a value passed through a validation error — has no mechanical backstop, so
the following still rests on judgment:

- **Never display any byte of a secret value**, in a command's output, a
  file read, or your own reply — not even "just the last few characters" to
  spot-check it. Verify a secret was written/transmitted correctly
  *structurally* instead: length (`wc -c`), presence (`grep -c`, or a
  key-names-only listing), or a hash comparison. Never a value comparison
  that requires printing the value to eyeball it. The same "no broad/
  unbounded command against a file known to hold secret material" caution
  applies to any non-`.env` secret-bearing file (the PEM, the GCP JSON) —
  those aren't hook-protected.
- **Never dump broad environment/config state.** `env`, `printenv`, bare
  `set`, Python's `os.environ`, or serializing a settings/config object
  wholesale (`print(settings)`, `settings.dict()`/`.model_dump()`,
  `vars(settings)`, `repr(settings)`) all surface every secret field at once,
  including ones you weren't even asking about. Read or pass individual
  fields programmatically instead, and reduce any secret-bearing value to a
  boolean/length/hash *before* it can reach a print statement, log line, or
  tool-result — mirroring `scripts/_render.py::env_vars()`'s documented
  contract and `scripts/deploy.py::sync_env()`'s
  `print(f"pushed {key} (len {len(value)})")` convention (name + length,
  never the value) — follow that same shape in any new ad hoc script that
  touches secrets.
- **Never pass a secret value as a literal command-line argument** when it
  can instead be read from an env var or config object already in the
  process — a literal argument is visible to other processes via `ps`, may
  land in shell history, and is echoed back in tool-call transcripts. For
  the same reason, do not enable verbose/debug HTTP logging (e.g. `curl -v`,
  httpx debug logging) while a real credential is attached to the request —
  it can print an `Authorization` header verbatim.
- **Never let a secret-holding field's validation error or exception
  traceback reach output un-redacted.** Some validators (e.g. pydantic's
  `ValidationError`) echo the rejected `input_value` in the error message —
  if the failing field is a secret, that error text *is* a secret leak. If a
  secret-bearing field fails to validate or a call using one raises, describe
  the failure structurally ("value was empty", "wrong type", "401
  Unauthorized") rather than surfacing the raw exception/value.
- **Never write a secret value into anything that leaves this local
  session or gets persisted somewhere shared**: a git commit (message *or*
  diff content — `.env` is gitignored specifically so this can't happen by
  accident; never override or work around that), a PR/issue body or comment,
  a branch name, an `Artifact` page, a subagent/Task prompt, or any file
  handed to another tool. Committing `.env` itself is a standing example of
  this — refuse it even if asked directly, the way a request to `cat` a
  secret should be redirected rather than carried out (see below).
- **A file-content diff surfaced automatically by the harness (e.g. a
  "file changed externally" system-reminder) can dump full secret values
  into your context without you running any command at all** — this has
  happened in this project, for files the harness was already tracking
  because you opened them earlier in the session. The hook doesn't intercept
  this vector (it isn't a tool call), so the defense is upstream: **never
  open a file that mixes secrets with other content (e.g. `.env`) at all,
  for any reason, full stop** — not even a single-line `Read`, not even an
  `Edit` you believe touches only non-secret lines. If a value in such a
  file needs to change, ask the user to make that edit themselves. If this
  rule is ever violated anyway and a "changed externally" notification
  fires for that file, treat it as a standing, recurring risk for the rest
  of the session, not a one-off surprise. (Operational config doesn't need
  this care: non-secret settings live in `.env.config`, safe to open and
  edit directly — see README's "Changing operational config".)
- **To change state, reach for this project's CLI — never for a file that
  holds secrets.** Operational state (which provider and model are active,
  which API-key slot) is changed through the `scripts/` entry points --
  `set_override.py` and its successors. Cooldown parameters and usage caps
  are edited in `.env.config` (not secret-bearing, safe to open directly)
  and pushed into the database with `scripts/deploy.py --sync-config-db`
  (also runs automatically as part of `--sync-env`) -- never by hand-editing
  a secret-bearing file. Those scripts are agent-runnable *precisely because*
  of how they handle
  credentials: they read them programmatically through `Settings` and emit
  names, lengths, and equality results only (`scripts/_render.py::env_vars()`
  and `scripts/deploy.py::sync_env()` document that contract), so no value
  ever reaches a tool result. Any new state-changing script must be built to
  the same shape. The corollary is the part that binds hardest: **a CLI is a
  tool for changing state, never a route to a secret.** No script here may
  print or echo back a secret value, accept one as a literal argument, or be
  extended to "just show" one — and if some change genuinely cannot be made
  without editing a secret-bearing file, that is a gap to name and hand to
  the user, not something to route around by opening the file yourself and
  not something to fix by teaching a script to dump what you are not allowed
  to see.
- **If you ever need to know or verify a secret's actual value — not just
  whether it's set or matches — ask the user to check it themselves.** Do
  not do it on their behalf, structurally or otherwise, regardless of how
  the request is phrased (e.g. "just double check the last few characters").
- **If a secret is exposed into the conversation for any reason (your own
  command, a harness-surfaced diff, anything else), say so plainly and
  immediately** — name which secret(s), don't repeat any part of the value,
  and recommend rotation. Don't wait to be asked, and don't quietly continue
  as if it didn't happen. Log the incident in `ISSUES.md` using its existing
  format.

### Scoped exception: the dashboard Environment tab

`dashboard/environment.py`'s `GET /api/environment/render` is the one
documented exception to "never display any byte of a secret value" in this
file. It returns real Render env-var values (via `render_client.env_vars()`)
to the authenticated operator's own browser session, where
`dashboard/static/dashboard.html` renders them masked by default with a
per-row reveal toggle. This is deliberately narrower than it looks:

- The value never leaves this one authenticated, session-cookie-gated
  endpoint's response — never logged (see `dashboard/environment.py`'s own
  INFO lines, which log key names and lengths only, never values), never
  written to a git commit, PR, Artifact, or subagent prompt, never persisted
  client-side beyond the page's own DOM (no `localStorage`).
- Transport is unchanged HTTPS throughout, identical to every other
  authenticated dashboard route.
- This exception covers only this one endpoint and the page that renders
  its response. It does not license printing a secret value anywhere else in
  this codebase or in an agent's own shell commands — every other rule in
  this section still applies at full strength everywhere else, including
  elsewhere in `dashboard/` and the rest of the root-level review engine.

See `docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md`
for the full design and the reasoning behind this carve-out.

## Project

Full design lives in `SPEC.md`; cost model in `cost.md`. Deployed as a Docker
container on Render (free tier) with the queue in Supabase Postgres, kept warm
by a free external pinger — see `cost.md` for the alternatives that were weighed.

## Module boundaries and contracts

### Layering constraints

- **Orchestrator** owns: diff prep (line annotation, token cap), fan-out, merge,
  formatting. Knows nothing about provider internals.
- **Specialists** are uniform (`run()`), differing only by **system prompt** +
  **Pydantic schema**. Each records its own timing + token usage. Know nothing about GitHub.
- **Providers** are swappable via `LLM_PROVIDER`; a shared validate-repair layer
  guarantees structured output regardless of provider.
- **Formatting** turns a `ReviewResult` into Markdown. Knows nothing about LLMs.

### Contracts

- Webhook handler: verify HMAC on the **raw body** → return **202 immediately** →
  run the review in a background task.
- Provider adapters normalize usage metadata (`tokens_in`/`tokens_out`) so cost
  can be computed from a single rate table (`pricing.py`).
- Provider adapter constructors (`GeminiProvider`, `VertexProvider`,
  `GroqProvider`) take an explicit `model: str` parameter and must never read
  `Settings` for the model internally. `providers/active_model.py` is the
  single resolver of "which model is active for this provider" (env default or
  DB override); adapters only ever bake in whatever model value they were
  constructed with. This is what keeps the model reported in the PR comment
  guaranteed equal to the model actually sent to the provider -- a regression
  back to an internal `Settings` read would silently break that guarantee.

## Conventions

- Async throughout; one-purpose modules with narrow interfaces.
- Secrets only via env vars; **no secret is ever logged**. See "Secret
  handling" at the top of this file for the full rule set — it is the
  highest-priority section and binds an agent's own ad hoc shell commands
  during manual/operational work, not just application code.
- **Never commit on someone else's behalf without being asked**, even to reach
  a clean working tree. If resolving a merge or other cleanup requires
  temporarily setting aside someone else's pre-existing uncommitted changes
  (e.g. via `git stash`), restore them **uncommitted**, exactly as found —
  committing them for tidiness is still an unrequested commit.
- **Partial failure is always visible** in the PR comment (a failed specialist
  renders a real row) — never silently dropped.
- **Before pushing, always run the full test suite (`uv run pytest -v`) and
  ruff (`uv run ruff check .`), and fix whatever either finds.** Never push
  with a red suite or an unresolved lint error, and never skip either check
  because a change "looks" too small to affect them.
- **After merging to `main` locally, always invoke the `deploy-verify`
  skill before pushing/deploying** — a green `pytest`/`ruff` run does not
  substitute for this (see the skill for why, and the incident it
  generalizes from).
- **When designing or changing a web page's UI (`dashboard/static/`), invoke
  the `ui-visual-review` skill before calling the work done** — reading
  HTML/CSS and reasoning about layout is not a substitute for actually
  rendering the page (see the skill for why, and the incident it
  generalizes from).

## Docker image: no `chown -R` (2026-09-07)

`Dockerfile` creates `appuser` and switches to it via `USER appuser` before
`CMD`, but **deliberately never runs `chown -R appuser:appuser /app`** (or
any other recursive chown of the whole app directory). This was a real
convention change, not always the case -- measured live: removing a prior
`RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app` line cut
the built image from 541MB to 384MB (~29%). The reason is mechanical, not
cosmetic: an overlay filesystem stores a changed file as a full copy, not a
diff, so `chown -R` over everything just `COPY`'d/`RUN uv sync`'d in
earlier layers (the whole `.venv` plus the app code) duplicates all of it
into a new layer. **Nothing under `/app` is ever written to at runtime** in
this service -- verified before removing the chown, not assumed (the only
filesystem access at runtime is a read-only `StaticFiles` mount) -- so
`appuser` only ever needs the read+execute permissions `COPY`/`RUN` already
leave in place by default; there was never a functional reason for the
ownership change to begin with. This was investigated because the
onboarding wizard's own deploy of this project felt slow on Render's free
plan -- `render_client.py`'s own hardcoded `dockerfilePath` (see the
2026-09-07 fix elsewhere in this history) is what actually builds this
Dockerfile for a wizard-provisioned service, so a smaller image here
directly shortens every visitor's "Finish & Deploy" step, not just local
`docker build` time.

**If a future change genuinely needs write access under `/app`** (a cache
file, a local SQLite DB, anything written by the running process rather
than read), re-verify that need is real, then chown *only that specific
path* (e.g. `RUN mkdir -p /app/some-dir && chown appuser:appuser
/app/some-dir`) or use `COPY --chown=appuser:appuser` on just the files
that need it -- never reintroduce a blanket `chown -R /app`, which pays the
full duplication cost again for the entire image regardless of how small
the actual write-needing path is.

`tests/test_dockerfile.py` pins both properties: no live `chown` command
anywhere in the file (comments explaining this tradeoff are fine -- the
check skips comment lines), and `useradd`/`USER appuser` still run in the
right order before `CMD`. It cannot verify the *size* claim (that requires
an actual `docker build`, which the `deploy-verify` skill already does as a
boot smoke test, not a size assertion) -- the test only guards against
someone silently reintroducing the anti-pattern, and its regression comment
carries the measured numbers for anyone re-evaluating this later.

## Impeccable comp-first image generation (manual bridge)

For dashboard redesign work via the Impeccable skill, comp-first image
generation is wired to Hugging Face's Inference Providers (fal-ai backend,
`black-forest-labs/FLUX.1-schnell`) rather than Impeccable's own
`generate-image` CLI command — that command only checks for
`OPENAI_API_KEY` and has no pluggable backend, so it will report image
generation as unavailable even though this bridge exists.

- **Script:** `~/.config/impeccable-hf/generate_image.py` — deliberately
  outside this repo (machine-local tooling, not a project dependency; no
  entry in `pyproject.toml`, nothing for other contributors to install).
  Usage: `python3 ~/.config/impeccable-hf/generate_image.py "<prompt>"
  <output.png> [--width W] [--height H]`.
- **Token:** `~/.config/impeccable-hf/token` — a Hugging Face access token
  with "Make calls to Inference Providers" permission, one line, mode 600.
  Not committed anywhere, not part of this project's own credential set
  (`GEMINI_API_KEY`/`GCP_SERVICE_ACCOUNT_KEY`/etc.) — same handling
  discipline as any other credential applies: never print it, never pass it
  as a literal argument, never let it reach a git commit/PR/Artifact.
- **Why the router path is hand-rolled and provider-specific:** HF's
  Inference Providers router (`router.huggingface.co`) proxies to each
  backend provider's own API rather than exposing one uniform REST shape.
  The script mirrors `huggingface_hub`'s `inference/_providers/fal_ai.py`
  (route `/fal-ai/<provider_model_id>`, payload `{"prompt", "image_size"}`,
  response is a JSON body with an image URL to download, not raw image
  bytes) because `huggingface_hub` itself isn't installed in this
  environment (no working `pip`) — this is why it isn't just
  `provider="auto"` via the official client.
- **If fal-ai stops serving this model:** check
  `https://huggingface.co/api/models/black-forest-labs/FLUX.1-schnell?expand=inferenceProviderMapping`
  for another provider with `"status": "live"`, then update the script's
  route/payload to that provider's own shape (they differ per provider —
  don't assume fal-ai's shape generalizes).
- Confirmed live end-to-end 2026-09-06: produced a real dashboard-shaped
  comp (nav/sidebar/stat-cards/data-table composition) at 512×512 in one
  deliberate call — same "one deliberate live call, no burst-testing"
  discipline as the LLM API testing hygiene rule below applies to this too,
  since it's still a third-party provider's free tier.

## Substitutions from the brief (and why)

- **`google-genai`** instead of the legacy `vertexai.generative_models` SDK —
  same Vertex backend, and it is what makes the one-env-var provider swap trivial.
- **`gemini-flash-latest`** instead of `gemini-2.5-flash` — the brief's model is
  deprecated/removed. The alias is pinnable to a dated version via env for demo
  reproducibility.
- **`vertex` adapter reinstated (2026-08-14)** — it was removed when Vertex AI's
  payment-card requirement collided with this project's no-card constraint (see
  `guide/background/providers.md`), leaving it live-unrunnable and mock-only.
  GCP billing/ADC access later became available, so `vertex` is back as a
  real, live-runnable third provider, matching `SPEC.md`'s stated default.
  Its credential is a GCP
  service-account identity rather than an API-key string:
  `GCP_SERVICE_ACCOUNT_KEY` (hosted, numbered slots, base64, verbatim only —
  see the 2026-08-16 credential-convention design) → implicit ADC, resolved
  in `providers/vertex_credentials.py`. No secret reaches Postgres — only
  the slot index, exactly as for gemini/groq.

## Cost

Documented production total ≈ **$8–10/mo** at brief scale (20 PRs/day). The demo
runs at **$0** on free tiers + the $300 GCP trial credit. Cost is graded as a
documented calculation, not as actual spend — see `cost.md`.

## LLM API testing hygiene (avoid Trust & Safety flags)

Gemini AI-Studio access got **account-level blocked** (`403 PERMISSION_DENIED:
Your project has been denied access`) during this build, confirmed across
multiple models, multiple projects, and multiple separate Google accounts —
per Google's own AI Developer Forum, this is an automated Trust & Safety flag,
and one documented trigger is **hitting repeated 429s / testing many models
back-to-back without backoff**, which is exactly what happened during
troubleshooting here. The only documented fix is attaching GCP billing, which
this project's setup deliberately avoids (see `guide/background/providers.md`)
— so once flagged, a provider is effectively lost for the rest of the demo.
(**Update, 2026-08-10:** a later API key update resolved this specific block
— see `guide/background/providers.md` — but that doesn't change the rule
below; a flag is still a real risk that this discipline exists to avoid, not
something to rely on being reversible.)
**Rules to avoid repeating this:**

- **Never loop/burst live calls across many models or keys** to "see what
  sticks." One deliberate, single live call per real verification need.
- **Prefer mocked/cassette tests for exploration.** Reserve real network calls
  for the one live-verification step a build step actually requires (per
  `SPEC.md` section 8's testing strategy) — not for debugging or model-shopping.
- **If a provider starts returning 403/429, stop calling it immediately** and
  investigate via docs/support channels rather than retrying with different
  models/keys in quick succession — retrying does not help and each attempt
  is one more data point that can reinforce an abuse-pattern flag. This
  extends to OAuth/auth-layer failures too (e.g. `invalid_scope`,
  `RefreshError`) — same failure shape, same stop-and-diagnose principle,
  not a "try a different scope/key" situation.
- **The "one deliberate live call" limit is about generation/completion
  requests** — the ones that cost money and carry provider-abuse-flag risk.
  It does **not** apply to lightweight metadata/listing calls (e.g. checking
  whether a model ID exists in a provider's catalog). Checking several
  candidate values via a listing/existence endpoint in one pass is fine, and
  is the right way to narrow down configuration *before* making the one
  deliberate generation call — not a workaround for the rule above.
- This applies to **any** LLM provider's free tier, not just Gemini — Groq and
  future alternatives should get the same restraint.

## Plan-execution / multi-agent process hygiene

Lessons from running Superpowers-style plans through subagent-driven
development on this project (see `ISSUES.md` for the incidents these
generalize from):

- **A task brief's "stop and report" instruction is a hard stop, not a
  suggestion.** If an implementer hits an unpredicted failure a brief says to
  stop on, it must actually stop and return control — not self-resolve the
  problem and mention the deviation in its report afterward. A controller
  reviewing a report after the fact cannot approve or reject work that has
  already been done; by the time it reads "I deviated because...", the
  deviation has already happened.
- **When correcting or overriding part of a multi-sentence passage, re-read
  the whole passage afterward for internal consistency** — not just the
  clause that was changed. A targeted fix to one sentence is exactly the kind
  of edit that leaves a contradiction elsewhere in the same passage
  undetected by the person who made it.
- **Task-scoped review checks conformance to the brief, not correctness of
  the brief itself.** Code a plan hands an implementer verbatim — especially
  for external-API/auth integration (credential construction, OAuth scopes,
  client setup) — needs the same scrutiny as any other code. Matching the
  brief exactly does not mean the brief was right; a bug embedded in a plan's
  own provided snippet will sail through every task-scoped review that only
  checks "does this match what was asked." **When a task's diff includes this
  class of code, run the `code-review` skill against that diff immediately,
  as part of finishing the task — not deferred to final/whole-branch
  review.** Final review is still a backstop (don't assume a whole-branch
  review is redundant just because per-task reviews already passed — it is
  often the first review that would even think to distrust the plan's own
  code), but it's a backstop, not the primary catch: the Vertex OAuth
  `scopes=` bug and the `list_vertex_models` SSRF both sailed through
  multiple per-task reviews before a final review caught them, which is
  exactly the delay this per-task trigger exists to close.
- **Documentation describing the outcome of a live-verification step must be
  written after that step actually runs, not drafted in advance assuming
  success.** If a plan's task text describes what a doc should say about a
  pending live call's result, treat that text as a placeholder to revise
  based on the actual outcome, not as literal instructions to transcribe.
- **When a plan is authored in the same session that will execute it via a
  worktree-based flow, write or commit the plan file *inside* the worktree**
  (or commit it to the branch before creating the worktree). Writing a file
  to the main checkout and then branching off via `git worktree add` leaves
  that file invisible to the new worktree, since worktrees only materialize
  committed content.
- **Before merging a feature branch into any target branch, check the
  *target* branch for pre-existing uncommitted changes first** (`git status`
  there, not just on the branch being merged in) — a conflicting local edit
  or untracked file on the target can fail the merge in a way that's
  confusing to debug from the merge failure alone.
- **Don't ask an implementer subagent to reconfirm a full-suite baseline at
  the start of every task.** Trust the SDD ledger's last-recorded green
  state from the prior task's own final run instead. The shared
  `subagent-driven-development` skill's implementer template already asks
  for exactly one full-suite run, right before committing — a controller
  adding its own extra "first, confirm baseline" instruction on top of that
  is a habit this project fell into in earlier stages, not something the
  template requires. For a plan's first task, the worktree-setup step that
  precedes dispatch is normally what already confirms things are green, so
  there's usually no real gap to fill even there. Reason: measured directly
  during the 2026-08-19/20 test-suite-performance work — the doubling was
  never principled, and the case for it is weaker still now that the suite
  itself is faster (full suite 57s serial → 35s at `-n 4`; the `-m "not db"`
  fast-iteration subset 31s → 20s — see
  `docs/superpowers/specs/2026-08-19-test-suite-performance-design.md`
  section 8). Only add an explicit baseline-reconfirm instruction when
  there's a concrete reason to distrust the ledger for *this* task
  specifically — manual edits since the last confirmed-green run, a resumed
  session after a long gap, or a worktree/branch switch — not as a default
  precaution on every task.
- **Every parked/deferred Minor finding from a task-scoped or final
  whole-branch review must be logged in `ISSUES.md`'s Parked Issues section
  before the branch is considered done** — not left only in the SDD
  ledger (deleted once the branch merges) or in a session's own memory,
  either of which loses the finding the moment the workspace is cleaned up
  or the conversation ends. Log it there even when a review explicitly
  judges a finding "no action needed" / harmless-as-is — that judgment call
  belongs in the entry's **Why parked** line, not as a reason to skip
  logging it at all.
