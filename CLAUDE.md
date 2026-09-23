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
- **Any ad hoc `gitleaks` invocation must always include `--redact`**,
  mirroring the pre-commit hook's own `gitleaks protect --staged --redact
  -v`. That hook is `--staged`-scoped and would never see an untracked
  credential file sitting on disk; a plain `-v` scan of the full working
  tree (e.g. to verify a `.gitleaks.toml` allowlist change) walks into
  exactly that risk and, without `--redact`, prints a real snippet of any
  matched secret's bytes straight into the command output — see
  `ISSUES.md`'s 2026-09-16 entry, where this happened on a real GCP
  service-account key.
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
- **Providers** are swappable via `runtime_config.provider` (DB-only, no env
  fallback — `scripts/set_override.py`); a shared validate-repair layer
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

### Cross-repo contract direction

**This project owns the schema contract; whoever provisions the database
owns the row.** This repo declares `runtime_config`/`slot_config`'s shape,
backfills any column it can derive, and widens the table itself at boot.
The provisioner (`TovTechOrg/onboarding-wizard`) writes only what it
uniquely knows: provider, key slot, model. The bot refuses to start if
*that* is missing but never refuses to start over a column it could have
filled itself. Keep this direction: **this repo's contract checks
(`contracts/provisioning.json`, generated by `scripts/gen_contract.py`) are
all local and never block on the consumer** — a contract change lands here
green on its own; the consumer's lag is reported by a scheduled advisory job,
never a blocking check. Also: **don't put a caller-set invariant only in a
store-layer docstring** ("the only caller always writes the full pair") —
validate it in a shared predicate every writer calls instead.
Full incident history and rationale: `docs/conventions/rationale.md#cross-repo-contract-direction-2026-09-10`.

**Push ordering when a change set spans both repos:** push here to `main`
first, then in `onboarding-wizard` run
`uv run python -m scripts.update_bot_contract` before committing and
pushing there. That script re-vendors `contracts/provisioning.json` and its
pin against this repo's `origin/main` — running it before this repo's own
push pins against a commit that's about to be superseded, and skipping it
after leaves onboarding-wizard's vendored copy silently behind whatever
this repo actually shipped.

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
- **Before pushing, always run the full test suite (`uv run pytest -q`) and
  ruff (`uv run ruff check .`), and fix whatever either finds.** Never push
  with a red suite or an unresolved lint error, and never skip either check
  because a change "looks" too small to affect them. Use `-q`, not `-v`, for
  a routine run — pytest still prints the full traceback for any failure
  either way; `-v`'s only effect is one extra line per *passing* test, which
  is pure Bash-output token cost on every green run and adds nothing when
  there's nothing to report.
  This rule covers `pytest`, `ruff`, and CI's blocking `lint-and-test`/`docs`
  jobs. It deliberately does **not** extend to
  `.github/workflows/consumer-contract-lag.yml`, the scheduled advisory
  consumer-lag job: a red run there means the consumer has not vendored this
  repo's latest contract yet, which is the normal, transient state between a
  contract change landing here and the consumer's catch-up commit. Gating a
  push on it would invert the ownership direction above and reintroduce
  exactly the deadlock the superseded reciprocal-pin design died of.
- **Before any push to `main`, always invoke the `deploy-verify` skill** —
  whether the commit reaching `main` arrived via a merge or was made
  directly, the risk this catches (a deploy image that builds/boots
  differently than the local dev venv) is the same either way. A green
  `pytest`/`ruff` run does not substitute for this (see the skill for why,
  and the incident it generalizes from).
  `deploy-verify` is deliberately not ledger-eligible — see
  `docs/conventions/rationale.md#which-repeatable-checks-are-ledger-eligible-2026-09-22`.
- **When designing or changing a web page's UI (`dashboard/static/`), invoke
  the `ui-visual-review` skill before calling the work done** — reading
  HTML/CSS and reasoning about layout is not a substitute for actually
  rendering the page (see the skill for why, and the incident it
  generalizes from).
- **The `.claude/hooks/` files are shared with the sibling repo and must stay
  byte-identical.** `~/pr-review-bot` and `~/onboarding-wizard` each carry
  their own copy of `check_env_access.py`, `redact_output.py` and
  `check_exfiltration.py`. Neither repo's CI can see the other, so nothing
  mechanical catches drift -- and `check_env_access.py` already drifted once,
  silently, leaving the wizard on the superseded pipe-based wrapper. Changing
  a hook in one repo means porting it to the other **in the same session**,
  verified with `diff <repo-a>/.claude/hooks/<file> <repo-b>/.claude/hooks/<file>`
  printing nothing, before either change is considered done. Per-repo
  differences belong in `check_exfiltration.py`'s `_PROTECTED` list, which was
  designed wide enough that nothing else should need one.

## Docker image: no `chown -R`

`Dockerfile` creates `appuser` and switches to it via `USER appuser` before
`CMD`, but **deliberately never runs `chown -R appuser:appuser /app`** (or
any other recursive chown of the whole app directory) — it cut the built
image ~29% (541MB → 384MB) because an overlay filesystem stores a changed
file as a full copy, not a diff, so a blanket chown over everything
`COPY`'d/`RUN uv sync`'d in earlier layers duplicates all of it into a new
layer. Nothing under `/app` is written to at runtime, so `appuser` only
ever needs the read+execute permissions already left in place by default.
**If a future change genuinely needs write access under `/app`**, chown
*only that specific path* (or use `COPY --chown=appuser:appuser` on just
the files that need it) — never reintroduce a blanket `chown -R /app`.
`tests/test_dockerfile.py` pins both properties (no live `chown`, correct
`useradd`/`USER` ordering) but can't verify the size claim itself — that
needs an actual `docker build`, which `deploy-verify` already does as a
boot smoke test. Full rationale and measurements:
`docs/conventions/rationale.md#docker-image-no-chown--r-2026-09-07`.

## Impeccable comp-first image generation (manual bridge)

For dashboard redesign work via the Impeccable skill, comp-first image
generation is wired to Hugging Face's Inference Providers (fal-ai backend,
`black-forest-labs/FLUX.1-schnell`) via `~/.config/impeccable-hf/generate_image.py`
rather than Impeccable's own `generate-image` CLI command, which only checks
for `OPENAI_API_KEY` and will incorrectly report image generation as
unavailable. Full usage, credential handling, and fallback instructions if
fal-ai stops serving this model: `docs/conventions/rationale.md#impeccable-comp-first-image-generation-manual-bridge`.

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
  `VERTEX_GCP_SERVICE_ACCOUNT_KEY` (hosted, numbered slots, base64, verbatim only —
  see the 2026-08-16 credential-convention design) → implicit ADC, resolved
  in `providers/vertex_credentials.py`. No secret reaches Postgres — only
  the slot index, exactly as for gemini/groq.

## Cost

Documented production total ≈ **$8–10/mo** at brief scale (20 PRs/day). The demo
runs at **$0** on free tiers + the $300 GCP trial credit. Cost is graded as a
documented calculation, not as actual spend — see `cost.md`.

## LLM API testing hygiene (avoid Trust & Safety flags)

Gemini AI-Studio access got account-level blocked during this build after
hitting repeated 429s / testing many models back-to-back without backoff —
a documented, automated Trust & Safety trigger. A later key update resolved
that specific block, but the risk it represents is still real. Full incident
narrative: `docs/conventions/rationale.md#llm-api-testing-hygiene-the-ai-studio-block-incident`.

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

## Workspace isolation: worktree vs inline

The redaction wrapper (`check_env_access.py` part 2) and the harness's
`EnterWorktree` isolation guard do not compose — every git command in an
`EnterWorktree` session gets refused once the wrapper is active, a bare
`git status` included. **Never use `EnterWorktree` while the wrapper lives.**
A worktree created manually with plain `git worktree add` and used from an
ordinary session runs git freely — the wrapper costs one *tool*, not the
workflow. Full measurement detail and worktree caveats (no `.env`/`.venv`
in a worktree, `ExitWorktree` won't clean up a manual one, never `EnterWorktree
--path` a manual worktree): `docs/conventions/rationale.md#workspace-isolation-measurement-detail-and-worktree-caveats`.

### Which to use

**Inline -- a plain feature branch in the main checkout -- is the default.**
Use it for single-task changes, and for anything needing real credentials or
the synced venv: running the app locally, `ui-visual-review`, `deploy-verify`,
and any run where `tests/test_config.py`'s three placement guards should
actually execute rather than skip. The existing rule about checking the
*target* branch for pre-existing uncommitted changes before merging binds
harder here, since there is only one working tree to collide in.

**A manual worktree (`git worktree add`, never `EnterWorktree`)** for SDD
plans, genuinely independent parallel tasks, and experiments that may be thrown
away. This path is already sanctioned: the `superpowers:using-git-worktrees`
skill describes itself as working "via native tools *or git worktree
fallback*". The existing rule about writing or committing a plan file *inside*
the worktree still applies — see the next section.

## Plan-execution / multi-agent process hygiene

Lessons from running Superpowers-style plans through subagent-driven
development. Each is stated in full, with the incident it generalizes from,
in `docs/conventions/rationale.md#plan-execution--multi-agent-process-hygiene-full-detail`
-- read that section before executing a plan. The three that cost the most
when missed:

- A task brief's "stop and report" instruction is a hard stop, not a suggestion — an implementer must actually stop, not self-resolve and mention the deviation afterward.
- Task-scoped review checks conformance to the brief, not correctness of the brief itself — run the `code-review` skill immediately on any task diff touching external-API/auth integration, don't defer to final review.
- Every parked/deferred Minor finding from a task-scoped or final whole-branch review must be logged in `ISSUES.md`'s Parked Issues section before the branch is considered done — including findings judged "no action needed."
