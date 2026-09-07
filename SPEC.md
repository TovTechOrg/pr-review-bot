# SPEC — Autonomous Code Review Engine (Project ד)

A server that listens for GitHub PR webhooks. When a PR is opened/reopened or
receives new commits, an **Orchestrator** fetches the diff and runs three
**Specialist Agents** (Security, Performance, Code Quality) in parallel, each
backed by an LLM call with a structured-output schema. The Orchestrator merges
their findings into a single Markdown comment and posts it back to the PR.
Runs in production on **Render** with **Supabase Postgres** for the durable queue.

## Confirmed decisions

| Decision | Choice |
|---|---|
| Prod deploy | Docker container on **Render** + **Supabase Postgres** for queue (free tier) |
| LLM providers | **Vertex (default) + free-Gemini + Groq** via `google-genai` |
| Model | **`gemini-flash-latest`** (brief's `gemini-2.5-flash` is deprecated; pinnable via env) |
| Structured output | Per-provider native schema + **shared Pydantic validate-repair** |
| PR triggers | **`opened` + `reopened` + `synchronize` + `ready_for_review`**, plus a base-branch-retargeting `edited` (edit comment in place); `closed` cancels a not-yet-run ticket instead — see section 13 |
| GitHub auth | **GitHub App** (JWT → short-lived installation token) |
| Build order | **Security specialist end-to-end first**, then Performance + Quality |
| Webhook processing | Verify HMAC → **202 immediately** → background task runs review |
| Diff handling | Whole annotated diff per specialist + **token cap + visible truncation** |
| Replay defense | Dedup on `X-GitHub-Delivery` UUID (bounded in-memory LRU) |
| Durable queue | **Supabase Postgres** (`FOR UPDATE SKIP LOCKED` claim, `ON CONFLICT` enqueue) |

---

## 1. Architecture overview

```
GitHub PR (opened / reopened / synchronize)
  └─▶ POST /webhook
        (1) read RAW body → verify HMAC-SHA256 (constant-time) → reject 401 if bad
        (2) dedup on X-GitHub-Delivery → 200 no-op if already seen
        (3) return 202 immediately  ← beats GitHub's ~10s ack timeout
        (4) BackgroundTask: run_review()
              ├─ GitHub App auth → installation token
              ├─ fetch PR unified diff (PyGitHub)
              ├─ annotate diff lines with file:line
              ├─ (cap + truncate if over token budget)
              ├─ asyncio.gather(security, performance, quality, return_exceptions=True)
              ├─ merge results (successes AND failures) + timing + token usage
              └─ find-or-create the bot comment (marker) → post/edit Markdown
```

**Why async is mandatory:** three LLM calls can exceed GitHub's ~10s webhook-ack
timeout; a slow synchronous handler causes GitHub to mark the delivery failed and
**redeliver**, triggering a duplicate review. The 15s target is measured to
*comment-appears*, not to HTTP response.

**Updated in section 12:** step (4) above (`BackgroundTask: run_review()`) is
now a durable Postgres ticket enqueue; a single serial dispatcher — not the
webhook request — is the one caller of the review pipeline. This absorbs
per-minute/daily rate limits from the live providers without changing the
steps *inside* a review (diff prep → fan-out → merge → comment).

## 2. Module layout

The repo is a `uv` workspace of two members: the root itself (the review
engine) and `dashboard/` (the ops dashboard); see the 2026-08-29
project-restructure design for why the dashboard is a separate package
rather than folded into one `app/`. (That design's third package,
`onboarding/` — a self-service setup wizard for other deployments — later
split off into its own standalone repo, and the review engine itself was
then flattened from a `bot/` subdirectory up to the repo root; see the
2026-09-05 standalone-repo-restructure design.)

```
main.py                      FastAPI app + lifespan (provider factory, dedup cache,
                             boot-time credential checks); GET /healthz; mounts
                             dashboard/'s router
config.py                    pydantic-settings — all env vars in one typed place
config_deps.py               FastAPI Depends() wrappers over Settings
webhook.py                   /webhook route; HMAC dependency; delivery dedup; 202 + BackgroundTask
hmac_verify.py                verify_signature(raw, header, secret) — hmac.compare_digest
github_app.py                 JWT (RS256) → installation token; fetch diff; find/create/edit comment
render_client.py              Render API client — find_service_id, env-var CRUD (shared by
                             scripts/deploy.py and dashboard/environment.py)
orchestrator.py               prepare diff → fan out → merge into ReviewResult
diff_utils.py                 annotate diff with file:line; token cap + truncation
formatting.py                 ReviewResult → Markdown comment (with bot marker)
specialists/
  base.py                    Specialist protocol + shared run() (calls provider + validate-repair)
  schemas.py                  Pydantic finding models + envelopes
  security.py                 system prompt + SecurityFinding schema
  performance.py               system prompt + PerformanceFinding schema
  quality.py                   system prompt + QualityFinding schema
providers/
  base.py                    LLMProvider protocol: async complete(system, user, schema) -> BaseModel;
                             RateLimited(retry_after) — raised on a 429 (section 12)
  google_genai.py             Vertex (vertexai=True) + Gemini (api_key) — one SDK, two clients
  groq.py                     OpenAI-compatible client, constrained-decoding structured output
  vertex_credentials.py        resolves Vertex's GCP service-account credential (numbered slots,
                             base64, implicit ADC)
  factory.py                  select provider by LLM_PROVIDER env; caches clients by (provider, key slot)
  active.py / active_model.py  DB-backed provider/model override, with an in-memory fail-safe cache
  key_index.py                 which numbered API-key slot is active per provider
  credentials.py               resolves the actual env var backing a provider's active credential
  registry.py                  single provider -> env-var-name mapping, shared by root app and scripts/
  validate.py                  validate-and-repair (one repair retry → typed empty-with-error)
  pricing.py                   per-provider/model rate table → est_cost_usd
  catalog.py                   live model-catalog listing per provider, for the dashboard's
                             guided credential setup/replace flow and model picker
review_queue/
  store.py                    durable Postgres ticket store: enqueue_or_update, claim_next_due,
                             defer, mark_done, recover_on_startup, get_ticket (section 12);
                             also owns the `reviews` history table read by dashboard/router.py
  dispatcher.py                single serial consumer: process_next_due, run_forever,
                             in-memory blocked_until gate (section 12)
  cooldown_config.py / usage_cap_config.py / review_draft_config.py
                               DB-backed operational settings (re-review cooldown, per-key usage
                             cap, draft-PR review toggle), each with the same fail-safe-cache pattern
scripts/                     operator CLI tooling — see scripts/*.py; representative entries:
  deploy.py                   pre-deploy checklist (credentials, provider health, pricing drift, ...)
  set_override.py              change active provider/model/key-slot from the CLI, no redeploy
  doctor.py                    read-only guided-setup status check (see guide/)
  seed_demo_pr.py               push fixtures/bad_code branch + open a real PR (the demo)
fixtures/
  bad_code/                   planted issues: hardcoded credential, N+1 query, magic number
  webhook_payloads/            signed request fixtures (valid / invalid / replay)
  llm_cassettes/                recorded provider responses for deterministic E2E
Dockerfile
dashboard/                   ops/demo dashboard, mounted in-process by main.py — never
                             deployed standalone
  router.py                   GET / static page + GET /api/dashboard JSON + GET /api/environment/*
  auth.py                     session-cookie login/logout; require_session dependency
  environment.py               Render env-var + runtime_config CRUD backing the Environment tab
tests/                       cross-package tests (guide/doctor consistency, the repo-tooling
                             PreToolUse hook, the review engine's own tests, ...); dashboard/tests
                             holds dashboard's own tests
.github/workflows/ci.yml     ruff lint + pytest (deterministic test layers 1–6) on push/PR
pyproject.toml                workspace root (uv) — merged [project] block for the review engine
                             plus the shared dev dependency group
render.yaml                    Render Blueprint — builds Dockerfile
```

**Rule:** each module has one purpose and a narrow interface — the orchestrator
knows nothing about provider internals; specialists know nothing about GitHub;
formatting knows nothing about LLMs.

## 3. Data model (Pydantic)

Field names match the brief exactly, plus a `file` field so the comment can render
`file:line` without a second round trip.

```python
# specialists/schemas.py
Severity = Literal["critical", "high", "medium"]

class SecurityFinding(BaseModel):
    severity: Severity
    file: str
    line: int
    description: str
    fix: str

class PerformanceFinding(BaseModel):
    type: str                 # e.g. "N+1", "missing-cache", "blocking-io"
    estimated_impact: str     # e.g. "high", "~200ms/req"
    file: str
    line: int
    suggestion: str

class QualityFinding(BaseModel):
    category: str             # e.g. "duplication", "naming", "magic-number"
    file: str
    line: int
    issue: str
    refactoring_suggestion: str

class SpecialistResult(BaseModel):
    name: Literal["Security", "Performance", "Code Quality"]
    status: Literal["ok", "failed"]
    findings: list[dict] = []          # serialized findings of the specialist's type
    error: str | None = None
    elapsed_ms: int
    tokens_in: int = 0
    tokens_out: int = 0

class ReviewResult(BaseModel):
    pr_number: int
    provider: str                      # active LLM_PROVIDER
    model: str
    results: list[SpecialistResult]
    total_elapsed_ms: int
    total_tokens_in: int
    total_tokens_out: int
    est_cost_usd: float | None   # None when the model has no rate entry
```

The `SpecialistResult` envelope is the **minimum each specialist must carry** so
the comment (section 6) renders fully with no extra GitHub/LLM calls.

## 4. Provider abstraction

```python
# providers/base.py
class LLMProvider(Protocol):
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...
```

- **`vertex`** (default): `genai.Client(vertexai=True, project=..., location=...)`,
  model `gemini-flash-latest` (pinnable via env),
  `config={"response_schema": schema, "response_mime_type": "application/json"}`.
  Free on the $300 GCP trial credit.
- **`gemini`**: `genai.Client(api_key=GEMINI_API_KEY)` — otherwise **identical call**.
  AI-Studio permanent free tier (~1,500 req/day). Zero logic difference from vertex.
- **`groq`**: OpenAI-compatible client; structured outputs via constrained decoding.
  Different vendor + model (Llama) → demonstrates true provider-agnosticism.

`factory.py` selects by `LLM_PROVIDER`. `validate.py` sits above all providers:
validate returned JSON against the schema; on failure, one repair retry ("return
ONLY valid JSON matching this schema"); if still bad, return a typed empty result
and mark the specialist failed. This handles the "malformed/off-schema model
output" case centrally.

**SDK substitution:** the brief names the legacy `vertexai.generative_models` SDK;
we use the current unified `google-genai` (same Vertex backend) because it is what
makes the one-env-var swap between Vertex and AI-Studio trivial. The brief's
`gemini-2.5-flash` is deprecated/removed, so the default model is the alias
`gemini-flash-latest` (currently Gemini 3.5 Flash), pinnable to a dated version.

## 5. Orchestrator + specialists

- **Diff prep** (`diff_utils.py`): fetch unified diff, annotate each added/changed
  line with `file:line`, so specialist `line` outputs are trustworthy. Enforce a
  token budget; if exceeded, truncate and set a `truncated` flag surfaced in the comment.
- **Fan-out**: `await asyncio.gather(sec.run(d), perf.run(d), qual.run(d), return_exceptions=True)`.
- **Merge**: for each result, if it's an Exception, wrap into
  `SpecialistResult(status="failed", error=str(e))`; else the specialist's own
  `SpecialistResult(status="ok", ...)`. A single specialist failing **cannot** drop
  the others or blank the comment.
- Each specialist = same `run()` shape, differing only in **system prompt** +
  **schema**. Each records its own `elapsed_ms`, `tokens_in`, `tokens_out`.

## 6. GitHub PR comment format

Posted as **one issue comment**, edited in place on `synchronize` (found via a
hidden marker). Failed specialists render a **visible** row — never silently dropped.

```markdown
<!-- ai-code-review-bot -->
## 🤖 Automated Code Review — PR #42
_3 specialists · gemini-flash-latest (vertex) · 11.4s · ~$0.0021_

### 🔒 Security — 2 findings
| Severity | Line | Issue | Suggested fix |
|----------|------|-------|---------------|
| 🔴 critical | `app.py:14` | Hardcoded API key | Move to env var / secrets manager |
| 🟠 high | `db.py:88` | Unsanitized input → SQL injection | Use parameterized query |

### ⚡ Performance — 1 finding
| Impact | Line | Issue | Suggestion |
|--------|------|-------|------------|
| 🟠 high | `views.py:52` | N+1 query in loop | `select_related()` / batch fetch |

### 🧹 Code Quality — ✅ no findings

### ❌ Performance check failed
> `DeadlineExceeded` — other checks completed normally.

---
<sub>Runtime 11.4s · 4,910 tok in / 780 tok out · est. $0.0021 · provider: vertex</sub>
```

Footer runtime + cost come straight from the `ReviewResult`: providers return usage
metadata (Vertex/Gemini/Groq all expose it); `pricing.py` maps tokens × active-provider
rate → `est_cost_usd`. Pricing is **optional**: a model with no entry in the rate
table yields `est_cost_usd = None`, and the comment simply omits its cost fragments
rather than failing — the rate table prices reviews, it does not gate which models
may run. `scripts/deploy.py`'s `pricing` check reports an unpriced model as a
non-blocking `WARN`.

## 7. HMAC webhook validation

- Secret in `GITHUB_WEBHOOK_SECRET` (container secret) — never in code, never logged.
- **Read RAW body before any JSON parsing**: `raw = await request.body()`; do NOT
  bind a Pydantic body model (that consumes/reparses first). Compute
  `hmac.new(secret, raw, sha256).hexdigest()`, compare to `X-Hub-Signature-256`
  with **`hmac.compare_digest`** (constant-time), then `json.loads(raw)`.
- **Reject** (missing/malformed/wrong sig): **401**, log a warning with delivery ID, stop.
- **Replay**: dedup on `X-GitHub-Delivery` UUID in a bounded in-memory LRU; seen →
  **200 "already processed"**, no re-review. Limitation: the in-memory cache does
  not survive restart (acceptable at demo scale; a KV/Redis store would harden it).

## 8. Testing strategy (deterministic-first)

Stack: `pytest`, `pytest-asyncio`, `httpx.AsyncClient` + `ASGITransport`, `respx`
(mock outbound HTTP), recorded LLM cassettes.

1. **HMAC unit** — valid, invalid, missing header, **replayed delivery-ID → 200 no-op**.
2. **Provider adapters** — mock backend HTTP; normalized output + usage; **malformed
   JSON → repair path → typed result**.
3. **Specialist schema** — good + off-schema model output through validate-repair.
4. **Orchestrator partial-failure** — mock one specialist raising; assert other two
   survive and merge yields a `status:failed` envelope (resilience checkbox).
5. **Comment formatter (golden file)** — fixed findings incl. a failed specialist →
   exact Markdown.
6. **E2E — offline/CI**: signed payload fixture + mocked GitHub + LLM cassettes →
   deterministic; asserts the comment body contains the seeded issues.
7. **E2E — live dry-run**: `scripts/seed_demo_pr.py` opens a real PR from
   `fixtures/bad_code/` (planted hardcoded credential + N+1 query + magic number);
   assert comment appears within 15s with expected findings. **This is the
   rehearsable demo.**

## 9. Deploy + cost model

- **Dockerfile**: `uvicorn main:app`; runs identical locally / on Render.
- **Production hosting**: Render web service (free tier, spin-down after 15 min
  idle) + Supabase Postgres for the durable review queue (free tier, pauses
  after ~7 days inactivity). A free cron pinger (cron-job.org / UptimeRobot,
  ~10 min interval) keeps both services warm.
- **Public URL**: Render assigns a stable public hostname (persists across restarts)
  → set once as the GitHub App webhook URL via the `scripts/deploy.py` registration
  script (one-time, no manual edits on restart).
- **Secrets/env**: `DATABASE_URL` (Supabase pooler connection string),
  `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (base64-encoded PEM, verbatim only),
  `GITHUB_TARGET_REPO` (required, comma-separated allowlist — `*` tracks every
  repo the App installation covers), `LLM_PROVIDER`, plus provider creds
  (`GROQ_API_KEY`, etc.).
- **Cost**: see `cost.md`. Documented production total ≈ $8–10/mo at brief scale;
  the demo runs at $0 on free tiers + the $300 GCP trial credit.

## 10. Build order (implementation session)

0. **Guided setup (interactive, BEFORE any code).** Claude walks the user through
   and verifies each prerequisite, and does NOT proceed to step 1 until they're done:
   - **GitHub App**: register the app; set webhook secret + permissions (PR read/write,
     contents read); subscribe to `pull_request` events; download the private-key PEM;
     install on a throwaway test repo; capture App ID + installation ID.
   - **GCP / Vertex**: create project, enable Vertex AI, `! gcloud auth application-default login`
     (run in-session via the `!` prefix); confirm the $300 trial is active.
   - **Hosting**: create a Supabase project (Session-mode pooler URL as
     `DATABASE_URL`) and a Render service from `render.yaml`; the Render URL is
     the stable public webhook target.
   - **Secrets hygiene**: create `.env` from `.env.example`; add `.env` + the PEM to
     `.gitignore` BEFORE the first commit; decide PEM-as-file-path vs base64 env value.
   Output: a filled `.env` + a `SETUP.md` checklist capturing the values and steps.

1. **Skeleton**: `config.py`, `main.py`, `Dockerfile`, `pyproject.toml`,
   `.github/workflows/ci.yml` (ruff + pytest); `/webhook` returns 202; `/healthz`
   returns 200; local run + CI green on the first PR.
2. **HMAC + dedup** (`hmac_verify.py`, `webhook.py`) + their tests. Reject/replay covered.
3. **GitHub App** (`github_app.py`): auth → fetch diff → find/create/edit comment.
   Verify against a real test PR.
4. **Provider layer** (`providers/*`) with `vertex` first + `validate.py` + tests.
5. **Security specialist END-TO-END** (`specialists/security.py`, `orchestrator.py`
   with a single specialist, `formatting.py`) → run `seed_demo_pr.py`, confirm a
   real comment appears within 15s. **Milestone: full path proven on a real PR.**
6. Add **Performance** + **Code Quality** behind the same interface; enable
   `asyncio.gather` fan-out + partial-failure merge + tests.
7. Add **`gemini`** and **`groq`** providers; live-swap demo.
8. README + final E2E (offline + live dry-run rehearsal).

## 11. Verification

- `pytest` green across all 7 test layers (section 8); CI runs layers 1–6 deterministically.
- `docker build` + local `uvicorn` boots; `/healthz` → 200; `/webhook` rejects an
  unsigned request (401) and no-ops a replayed delivery (200). GitHub Actions CI
  (`ruff` + `pytest` layers 1–6) is green on the PR.
- The deployed Render URL is set as the GitHub App webhook; a manual GitHub
  "Redeliver" of a `pull_request` event produces a comment.
- **Live rehearsal**: `uv run python -m scripts.seed_demo_pr` opens a PR with the three
  planted issues; the bot comment appears within 15s naming the hardcoded credential,
  the N+1 query, and the magic number; footer shows runtime + cost; provider swap
  (`LLM_PROVIDER=groq`) still produces a valid comment.

## 12. Review queue (RPM + daily-quota handling)

Full design rationale (problem statement, alternatives considered, accepted
costs): `docs/superpowers/specs/2026-07-27-queue-features-design.md`. This
section documents what was actually built.

**Problem.** The live providers' free tiers have real caps — Groq ≈ 30 RPM /
14.4K per day, GitHub Models ≈ single-digit RPM / ~150 requests per day — and
the original design fired 3 concurrent LLM calls per review straight from a
per-request `BackgroundTask` with zero coordination across PRs.

**Producer/consumer split.** `webhook.py` no longer runs any LLM work: it
verifies HMAC, dedups the delivery, and calls
`store.enqueue_or_update(...)` to upsert a durable Postgres ticket, then returns `202`
immediately. A single serial dispatcher (`review_queue/dispatcher.py`,
`run_forever`) is started as an `asyncio` task from the app lifespan
(`main.py`) and is the **only** caller of the review pipeline — this
serializes every pacing/quota decision, and serial dispatch is anti-burst by
construction.

**Re-checked 2026-08-11 (performance audit):** the current polling cadence
(dispatcher's ~1s idle sleep between `claim_next_due` attempts; the
dashboard's 4s client-side poll of `/api/dashboard`) and this single-serial
dispatch design were both reviewed during the full-project audit and
confirmed correct as deliberate tradeoffs at free-tier scale and per the
Trust & Safety pacing discipline in CLAUDE.md — no change made.

**Durable Postgres ticket, one per PR.** `review_queue/store.py` keeps one row per
`(repo_full_name, pr_number)` (`repo_full_name` from the incoming webhook payload,
optionally narrowed by `GITHUB_TARGET_REPO`'s allowlist) with a `UNIQUE` constraint.
The same module also owns a `reviews` table (one insert-only row per completed
review — provider, model, timing, tokens, cost, and findings) that backs the
`GET /` / `GET /api/dashboard` ops/demo page (`dashboard/router.py`); it
is separate from `tickets`'s queue-lifecycle bookkeeping and from the
single-row `runtime_config` provider-override table.
`enqueue_or_update` applies a single per-state re-review policy (full design rationale:
`docs/superpowers/specs/2026-07-28-dispatcher-followups-design.md` §6):
a push to a **`pending`** ticket updates `head_sha` and stays `pending`
(unreviewed, so no cooldown applies); a push to a **`deferred`**/**`retrying`**
ticket **rides out** — `head_sha` is updated but `status`/`not_before` are left
untouched, so a push can never shorten a provider's rate-limit wait or an
in-progress cooldown; a push to a **`running`** ticket updates `head_sha`
and sets a `rereview_requested` dirty flag (no task cancellation), so
`store.finalize_review` re-arms that ticket for exactly one coalesced
follow-up review of the latest commit once the in-flight run completes; a
push to a **`done`/`failed`** ticket re-arms via the `_due_after_cooldown`
helper — `attempts` resets to 0, and the ticket lands on `pending`
immediately or `deferred` until the per-PR cooldown
(`DISPATCHER_REREVIEW_COOLDOWN_SECONDS`, default 300s, keyed on
`last_reviewed_at`, the timestamp of the last *completed* review) elapses.
While a re-review waits out the cooldown, a self-cleaning "re-review
scheduled ~HH:MM UTC" footnote is shown below the preserved review (see
below) rather than staying silent.
This cooldown now **escalates** per PR — a `cooldown_level` raises the
effective wait geometrically (`effective_cooldown(level) = min(base·factor^level, cap)`)
for a PR that keeps being pushed inside each window, resetting to 0 once the PR
stays quiet for a full window. Level 0 equals the base cooldown, so normal PRs
are unchanged; escalation only lengthens `not_before` (the schedule notice's
ETA reflects it automatically); at the defaults (factor 2, cap 3600s) it bounds
a churning PR from ~288 to ~26 reviews/day without ever abandoning it. The two
escalation sites are: (1) `enqueue_or_update` done/failed re-arm, and
(2) `finalize_review`'s dirty-flag branch.

**Tuning base/cap/factor.** All three are `.env.config` settings
(`DISPATCHER_REREVIEW_COOLDOWN_SECONDS` default 300s,
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` default 3600s,
`DISPATCHER_REREVIEW_COOLDOWN_FACTOR` default 2.0, must be `>= 1.0`) that live
in the same `runtime_config` singleton row the LLM-provider override already
uses (`scripts/set_override.py`), not a Render env var — the dispatcher reads
them from the database only, never from `Settings`, so there is exactly one
live value at all times (see the 2026-08-17 "two sources of truth" design
note). `scripts/deploy.py --sync-config-db` pushes `.env.config`'s current
values into that row with no redeploy (also runs automatically as part of
`--sync-env`); it takes effect on the next claimed ticket. A row that comes
back invalid (`factor < 1.0`, or `base > cap`) is discarded as a whole triple
and falls back to `review_queue/cooldown_config.py`'s built-in defaults —
mirroring `providers/active.py`'s fail-safe cache pattern.

**Swapping API-key slots.** Each provider's credential env var can have
numbered siblings (`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ...),
provisioned like any other env var (one redeploy to add a slot). A separate
`runtime_config` override per provider (`gemini_key_index`, `groq_key_index`,
`vertex_key_index`) records which slot is active; `NULL` means index
0, the base env var. `scripts/set_override.py` writes it — the same
no-redeploy, next-claimed-ticket mechanics as the provider/cooldown
overrides — and no secret ever reaches Postgres: only the integer index
does. `providers/factory.py` keys its client cache by `(provider,
index)`, so a swap invalidates exactly the right cached SDK client rather
than the whole cache. `scripts/deploy.py`'s `api-key-live` check is the
read-only counterpart, mirroring `provider-live`: it confirms the actively-
resolved index's env var is genuinely present on the live Render service.

**Proactive per-key daily usage cap.** The rate-limit handling above is
*reactive* — it waits for a real 429. `KEY_USAGE_TOKEN_CAP` adds a
*proactive* ceiling: before starting a review, the dispatcher sums
`total_tokens_in + total_tokens_out` over the `reviews` rows belonging to
the currently-active `(provider, key slot)` since the last
`KEY_USAGE_RESET_TIME_UTC` boundary (default `04:00` UTC, any
`HH:MM`/`HH:MM:SS` granularity). At or over the cap, the ticket is deferred
to the next reset instead of run — no call is made at all. The cap is unset
by default, so a deployment that does not set it in `.env.config` is
unaffected. A dollar-denominated cap (`KEY_USAGE_COST_CAP_USD`) existed
until 2026-08-18 and was removed: it rested on rate-table values the code
itself calls representative, so an understated rate made it fail *open* —
a ceiling an operator believed in but that did not hold. Token counts come
straight from the provider's usage response and are exact. Express a dollar
budget by dividing by the rate once, at config time, and setting a token
cap. Like the cooldown settings above, these two live only in the
`runtime_config` row
(`scripts/deploy.py --sync-config-db` pushes `.env.config` into it) — never a
Render env var. Usage is *derived*
from the persisted `reviews` history rather than counted in memory, so a
restart or redeploy never resets or loses it; a new `reviews.key_index`
column records which slot paid for each review, so swapping slots with
`scripts/set_override.py` immediately grants a fresh budget with no
special-case code — for the next ticket claimed; a ticket already deferred
by the cap still waits for its scheduled reset (raising or clearing the cap
doesn't retroactively release it). The check is deliberately check-before, not
predict-before: a review's real usage is only known once it completes, so
the cap bounds when the *next* review may start, not the exact daily total —
the same shape the reactive backoff already has. It also **fails open**: any
error while checking logs and proceeds as "not capped", because a broken
usage query must never be able to block every review. A capped ticket's PR
notice is deliberately distinguishable from a provider wait (a new
`tickets.defer_reason` column carries the distinction to the later notice
sweep), so an operator debugging a stalled review isn't sent hunting at the
provider for a limit this app imposed on itself. Full design rationale:
`docs/superpowers/specs/2026-08-15-key-usage-cap-design.md`.

**Re-review scheduled notice.** Rather than a fully silent wait, a deferred
ticket with a visible prior review gets a self-cleaning footnote —
`formatting.format_schedule_notice(not_before)`, "🔄 Re-review scheduled
~HH:MM UTC" (absolute time only; GitHub can't localize a comment per
viewer, and a relative string would go stale since this note is only
edited on a re-arm event) — appended via `github_app.append_schedule_notice`
below the review, delimited by `SCHEDULE_NOTE_START`/`SCHEDULE_NOTE_END`.
Posting/refreshing happens in a dispatcher-loop step,
`post_pending_notices(now)`, run once per `run_forever` iteration alongside
`process_next_due`: it queries `store.tickets_needing_notice(now)` —
`deferred` tickets with a visible review whose `not_before` has moved since
the marker column `notice_not_before` was last set — and calls
`store.mark_notice_posted` after each successful post. This single sweep
covers every trigger that puts a reviewed ticket into `deferred` (cooldown
re-arm, whether from a webhook push or `finalize_review`'s dirty-flag
branch, and a rate-limited wait with a good review already present — the
placeholder mechanism below only fires when no review exists yet) with one
code path, since the webhook process that runs `enqueue_or_update` does no
GitHub work by design. `defer_failed` (hard-failure retry backoff) sets a
distinct status, `'retrying'`, instead of `'deferred'`, so this sweep can
never mistake a silently-retrying ticket for a scheduled one —
`claim_next_due` and `enqueue_or_update`'s ride-out branch treat `'retrying'`
identically to `'deferred'` for claimability and push handling. The moment
a ticket is claimed, `process_next_due` strips any live schedule footnote
(`github_app.clear_schedule_notice` + `store.clear_notice`) before doing
anything else — the wait is over regardless of what happens next — and
`_strip_existing_footnote` recognizes either footnote kind, so whichever
footnote-writing call runs next self-heals a stale leftover of the other
kind even if a strip attempt failed.

`claim_next_due` claims the oldest due ticket (`pending`, or `deferred`/`retrying`
whose `not_before` has passed) using an atomic
`SELECT ... FOR UPDATE SKIP LOCKED ... WHERE status IN ('pending', 'deferred', 'retrying') LIMIT 1`,
so a claimed ticket cannot be re-claimed (the row lock prevents concurrent claims).
`enqueue_or_update` uses a transactional `SELECT ... FOR UPDATE` + `ON CONFLICT`
pattern to check and update the row atomically — the `FOR UPDATE` lock ensures
the read-check-write is atomic against other `enqueue_or_update` calls and against
`claim_next_due`, even if called from the event loop or moved to `asyncio.to_thread`
in the future. The Postgres row-level locking and transaction semantics guarantee
safety: no deadlock risk (Postgres detects circular waits and aborts), lock waiters
are fair, and transactions are brief (no `await` inside the transaction body).
`ON CONFLICT` handles the natural case where a ticket row may be created by
`enqueue_or_update` or already exist (upsert without a separate DELETE/INSERT).
Compared to SQLite's `BEGIN IMMEDIATE`, this is simpler (no manual begin/commit/rollback,
no busy-timeout handling) and scales: read-only replicas can serve `claim_next_due`
queries in the future without code change.

**Reactive detection, no caps.** Adapters (`providers/base.py` +
`google_genai.py`/`groq.py`/`github_models.py`) raise `RateLimited(retry_after)`
only on an actual `429`, parsing `Retry-After` (seconds or HTTP-date) via
`parse_retry_after`, falling back to `DEFAULT_RETRY_AFTER_SECONDS` (default
`60`) when the header is missing or unparseable. No per-provider RPM/RPD
number is hardcoded anywhere — a short `retry_after` behaves like a
per-minute limit, a long one like a daily wall; the code does not
distinguish them.

**Atomic reviews.** `orchestrator.attempt_review()` returns
`ReviewCompleted(review)` or `ReviewRateLimited(retry_after)`: if any of the
three specialist calls raises `RateLimited`, all partial results are
discarded and no comment is posted — the max `retry_after` across the
rate-limited calls is returned. `run_review()` remains as a
backward-compatible wrapper (raises `RateLimited` on the rate-limited case)
so existing scripts/tests are unaffected.

**Failure backoff + hard stop.** Two waits are kept separate end-to-end so a
provider-wide throttle and a single poisoned ticket never share a clock. A
`RateLimited` outcome (or the pre-flight `blocked_until` gate firing) defers
the ticket via `store.defer_rate_limited` — per-provider, floored at
`DISPATCHER_MIN_RETRY_AFTER_SECONDS` (default 1.0s) so a degenerate
`Retry-After: 0` or already-past HTTP-date can't tight-loop — and does
**not** count toward the hard stop, since a provider eventually frees up on
its own. Any other exception from `attempt_review` is a hard failure:
`store.defer_failed` increments the ticket's per-ticket `attempts` and the
dispatcher computes the next wait with the pure, unit-tested
`compute_backoff(attempts, jitter) = min(BASE * 2**(attempts-1), CAP) +
jitter()` (`DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS` default 2.0,
`DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS` default 300.0). `jitter()` comes
through an injectable module-level seam, `dispatcher._jitter()`, returning a
value in `[0, DISPATCHER_BACKOFF_JITTER_SECONDS]` (default 0.0 —
deterministic/off in tests and single-instance operation; a future
multi-instance deployment can set it above 0 to spread retries without a
code change). Once a ticket's hard-failure count reaches
`DISPATCHER_MAX_FAILURE_ATTEMPTS` (default 5), the dispatcher calls
`store.mark_failed` instead of deferring again and posts a marker-prefixed
`formatting.format_failure(pr_number, attempts)` comment naming the attempt
count — no raw exception text, per this project's secrets-hygiene rule —
satisfying "partial failure is always visible" for the terminal case. A plain
successful completion (no mid-run push) leaves `attempts` unchanged on the
now-`done` row — harmless, since that count is never read again unless the
ticket re-arms. `attempts` is explicitly reset to 0 in the two cases that
actually re-arm a ticket: `finalize_review`'s dirty-flag branch, when a
mid-run push coalesces into an immediate re-review, and a fresh push to a
`done`/`failed` ticket handled by `enqueue_or_update`'s terminal-state
branch.

**Never downgrade a good visible review.** A ticket's own
`last_reviewed_at` (set only by `finalize_review` on a genuinely successful
completion) is the signal that a real review is currently on the PR — a
tiny guard, `dispatcher._has_visible_review(ticket)`, checks
`last_reviewed_at is not None`. Two places used to overwrite that good
review unconditionally with something strictly worse; both now check the
guard first. At the terminal hard-stop (once `attempts` reaches
`DISPATCHER_MAX_FAILURE_ATTEMPTS`): if no good review is present, the
dispatcher overwrites the marker comment with
`formatting.format_failure(pr_number, attempts)` as before; if a good
review **is** present, it instead calls `github_app.append_review_footnote`
to append a sub-marker-delimited (`FAIL_NOTE_START`/`FAIL_NOTE_END`)
footnote below the preserved review, via
`formatting.format_failure_footnote(attempts)` — a repeated terminal
failure replaces the prior footnote in place (no stacking), and the next
successful review's `upsert_comment` overwrites the whole comment body, so
the footnote disappears on its own with no separate cleanup. This also
closes a silent-double-failure gap: the notice (overwrite or footnote) is
now posted **before** `store.mark_failed`, and if posting itself raises,
the ticket is **not** stranded as terminal — it goes through
`store.defer_failed` with the usual `compute_backoff` instead, so it keeps
retrying (and reattempts the notice) until visibility is actually restored.
Both `format_failure` and `format_failure_footnote` pluralize correctly
("1 attempt" / "N attempts").

**Placeholder → result, edited in place.** A ticket that can't run now (soft
`blocked_until` gate, or a fresh `RateLimited`) gets a placeholder comment —
`formatting.format_placeholder()` — posted through the same marker-based
`upsert_comment` used for real results, **unless** a good review is already
present (`_has_visible_review`), in which case the placeholder is
suppressed — the existing good review stays up, with the schedule-notice
sweep (above) appending its footnote instead of a fully silent wait — until
a later successful re-review overwrites the whole comment in place. (A
first-ever review, with `last_reviewed_at` still `None`, always gets the
placeholder — it is the only signal available at that point.) The real
comment later overwrites the placeholder in place, found via the existing
bot marker (no separate tracking needed for this). Wording varies by wait
magnitude: short waits say a rate limit was hit and the review will appear
shortly; waits at or above `PLACEHOLDER_DAILY_THRESHOLD_SECONDS` (300s) name
a daily quota and show an ETA computed from `now + retry_after`.

**In-memory `blocked_until` gate.** The dispatcher keeps a per-provider
`blocked_until` timestamp, learned only from the most recent
`RateLimited.retry_after`, so it doesn't fire calls it already knows will
fail. It is a soft optimization only — it is not persisted, and after a
restart it starts empty. What actually prevents an early run, restart or
not, is each deferred ticket's own durable `not_before`.

**Restart recovery.** At lifespan startup (`main.py`), before the
dispatcher starts: `store.recover_on_startup()` resets any `running` ticket
(interrupted mid-review by a crash) back to `pending`, also clearing a
`rereview_requested` flag if one was set (the fresh `pending` review already
covers the latest commit, so the flag is moot); `deferred`/`retrying` tickets
are left as-is, gated by their persisted `not_before`. The dispatcher then simply
drains whatever is due.

**Config** (`config.py`): `DATABASE_URL`
(Postgres/Supabase connection string, required for production),
`DEFAULT_RETRY_AFTER_SECONDS` (default `60`), `DISPATCHER_IDLE_SLEEP_SECONDS`
(default `1`), `DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS` (default `2.0`),
`DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS` (default `300.0`),
`DISPATCHER_MAX_FAILURE_ATTEMPTS` (default `5`),
`DISPATCHER_MIN_RETRY_AFTER_SECONDS` (default `1.0`),
`DISPATCHER_BACKOFF_JITTER_SECONDS` (default `0.0`, off),
`DISPATCHER_REREVIEW_COOLDOWN_SECONDS` (default `300.0`),
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` (default `3600.0`),
`KEY_USAGE_TOKEN_CAP` (default unset — cap off),
`KEY_USAGE_RESET_TIME_UTC` (default `04:00`). The last two are the
one set of *per-key* caps here; every other *numeric* var above is a pacing
knob (`DATABASE_URL`, first in the list, is a connection string, not one).

**Robust comment identity.** The bot identifies its own comment by the
persisted `comment_id` first, falling back to an author-filtered marker scan
(`user.type == "Bot"` + marker), so a human/other comment containing the
marker is never edited by mistake. `store.set_comment_id` persists whatever
id `github_app` actually returns from **every** route that finds/creates/edits
a comment, not only a fully successful review's `finalize_review` — the
placeholder, schedule-notice, and terminal-failure paths all persist it too
(2026-08-21 pre-flight audit; previously only a full success did, so a
comment touched by any other route could silently drift from the id on
file). If the bot's comment is deleted and a later footnote/notice call has
to recreate it, the id GitHub returns differs from the one on file
(`dispatcher._comment_was_recreated`); that mismatch nulls `last_reviewed_at`
(`store.clear_visible_review`) rather than let unrecoverable content keep
being treated as a "visible" review by the cooldown/placeholder-vs-footnote
logic above. The claim-time schedule-notice clear (`clear_schedule_notice`)
detects loss differently rather than via that same mismatch check: unlike
`append_schedule_notice`/`append_review_footnote`, it never creates a
fallback comment when none is found — it returns `None` outright — so a
confirmed-gone comment there is `comment is None` (with a `comment_id` still
on file), not an id mismatch; found missing in an independent review
(2026-08-21) as the one comment-touching path that hadn't been wired up
this way. The column is also available for the design doc's §13 "ping
comment" future feature, which remains out of scope.

**Out of scope** (mostly unchanged from the design doc, all deliberate — see
below for the one exception): provider failover on a daily wall, a priority
scheme (FIFO is sufficient), and horizontal scaling (single process, single
dispatcher; the atomic ticket claim would make multi-instance possible later
but it is neither built for nor tested). One item this list previously
named — *proactive quota accounting* — was deliberately reopened and built:
see "Proactive per-key daily usage cap" above. What remains out of scope
within it is the provider-reported half — no `x-ratelimit-*` header
tracking, no knowledge of the provider's own limits; the cap is entirely
self-imposed and locally computed.

**Testing.** Extends section 8's deterministic-first strategy with new
layers, all using an injected clock (no real sleeps): ticket store
(`tests/test_queue_store.py`), provider `RateLimited` parsing
(`tests/test_provider_rate_limited.py`), atomic rate-limit propagation
(`tests/test_orchestrator_rate_limited.py`), placeholder rendering
(`tests/test_placeholder_formatting.py`), the
schedule notice (`tests/test_schedule_notice_formatting.py`), the
dispatcher's burst/daily-wall/
restart-recovery behavior (`tests/test_dispatcher.py`), and the webhook's
enqueue path (`tests/test_webhook.py`). One live-verification item remains
per `CLAUDE.md`'s hygiene rules: confirming GitHub Models actually sends a
usable `Retry-After` header on a `429` (one deliberate call) — not yet
performed; until it is, the `DEFAULT_RETRY_AFTER_SECONDS` fallback is what
governs that provider's backoff.

## 13. PR lifecycle edge cases (2026-08-21 pre-flight audit)

A pre-flight audit ahead of the first real-production run against live
GitHub repos traced several PR lifecycle scenarios the original design
didn't cover. Full reasoning and alternatives considered for each:
`ISSUES.md`'s "Design Gaps" section, which recorded them as they were found
and closed — folded into this doc as they're settled, since these are now
just how the system behaves, not open questions.

**Cancellation on close/merge.** `closed` (GitHub sends the same action for
both a merge and a plain close) cancels any `pending`/`deferred`/`retrying`
ticket for that PR (`store.cancel_ticket`) rather than letting it run to
completion and post a comment on a PR that's no longer actionable. A
`'running'` ticket is left alone — by the time a closure could be observed,
`attempt_review` has already committed its spend and posted its comment,
and there is no cancellation token threaded through
orchestrator/specialists to abort it mid-flight. A later `reopened` revives
a `'cancelled'` ticket through the same terminal-state re-arm path
`enqueue_or_update` already uses for `'done'`/`'failed'`.

**Draft PRs skipped by default.** `REVIEW_DRAFT_PRS` (default `false`,
database-only override — same `runtime_config` mechanism as the re-review
cooldown/usage-cap settings, no redeploy to flip) controls whether a draft
PR gets reviewed. `orchestrator.attempt_review` checks the PR's *live*
`draft` status — fetched for free off the same `PullRequest` object the
diff fetch already needs — and short-circuits to `ReviewSkipped` (no
specialist call, no comment, no ticket left behind) when the flag says
drafts are skipped. `ready_for_review` joins the PR-triggers set
specifically because it's the only action that fires independent of a
push, which is what lets a draft marked ready with zero new commits still
get reviewed. Checking live status at dispatch time, rather than a
snapshot from whichever webhook action produced the ticket, means
`converted_to_draft` needs no separate webhook handling of its own.

**Repo rename tolerated; cross-org transfer already bounded.** A same-org
rename never errors (GitHub redirects old-name API requests transparently)
but silently changes which name every future webhook reports —
`fetch_pr_diff` surfaces GitHub's own canonical name for free (already
resolved internally to serve the diff request), and a mismatch against the
name a ticket is keyed on triggers `store.migrate_repo_rename` to move that
PR's `tickets`/`reviews` rows to the new name. A transfer to an org the App
isn't installed on genuinely 404s/403s (unlike a rename, this one really
does error) — already covered by the existing hard-failure backoff, no new
code needed; GitHub also stops delivering webhooks for a repo outside the
installation's coverage, so no further waste accrues either way.

**Base-branch retarget triggers a re-review.** `pull_request.edited` fires
for title/body edits too, which must not trigger anything — only a base
change can change the effective diff. GitHub's `changes` object on every
`edited` delivery names exactly what changed, keyed by field name, so a
`changes.base` key unambiguously identifies a retarget
(`webhook._is_base_retarget`). The diff itself was already computed against
the live base on every `fetch_pr_diff` call (never a stored/stale one), so
this doesn't fix a wrong-diff bug — it closes the gap where nothing
prompted a re-check after a retarget with no new commits of its own.

**Empty diffs skipped entirely.** A diff with no substantive content (e.g.
a zero-file merge commit) short-circuits to `ReviewSkipped` before any
specialist call, comment post, or `reviews` row — the same mechanism as the
draft-PR skip above. The dispatcher's `process_next_due` handles
`ReviewSkipped` via `store.discard_skipped_ticket`, in priority order: (1) a
push that landed on the same PR while the ticket was being processed
(`rereview_requested`) always wins — that possibly-non-empty push must not
be lost, so the ticket is reset to `'pending'`; (2) a ticket never reviewed
before (`last_reviewed_at IS NULL`) is deleted outright, leaving no comment
and no ticket trace at all; (3) a ticket that WAS reviewed before instead
reverts to `'done'`, preserving `last_reviewed_at`/`comment_id`/
`cooldown_level` untouched (2026-08-21, found by an independent review of
this same work) — deleting it in this case would silently reset the
re-review cooldown escalation for a PR already flagged as churny, which a
dummy empty-diff/draft push could otherwise be used to exploit deliberately.
This is deliberately not `finalize_review`: that always stamps
`last_reviewed_at`/`comment_id` to *now*, which would falsely mark a
nonexistent review as fresh/"visible" to the preservation logic above.

**Fork PRs and force-pushes: confirmed non-issues, no code changed.** A PR
is always addressed through the *base* repo's API
(`/repos/{base-owner}/{base-repo}/pulls/{n}/...`) regardless of whether its
head lives in a fork — `fetch_pr_diff`/`upsert_comment` never touch the fork
directly, and GitHub computes the cross-repo diff server-side, so the
installation token's base-repo scope (`contents: read` /
`pull_requests: write` / `issues: write`) is sufficient for both reading the
diff and posting the comment with no special handling. A force-push is
indistinguishable from any other push: `synchronize` fires identically for
both, `head_sha` is stored purely for record-keeping and is never compared
against anything, and `fetch_pr_diff` always fetches the diff fresh against
whatever the current head is — there is no incremental/cached diff logic
anywhere for a rewritten history to invalidate.

**GitHub App installation id: always required, never guessed.** Also
confirmed load-bearing during this audit (a real gap, not a
non-issue): `GITHUB_APP_INSTALLATION_ID` must be set explicitly and is
verified against the App's actual installation at every checkpoint —
deploy-time check, process startup, and reactively on the first hard
review failure — rather than optionally auto-discovered once at boot. A
confirmed-invalid installation now terminates the process (`os._exit(1)`)
so the host platform restarts into the same loud startup check, rather than
silently continuing under a dead credential or self-patching to a new one
mid-run. Full setup/capture instructions:
`guide/setup/03-install-app.md`.
