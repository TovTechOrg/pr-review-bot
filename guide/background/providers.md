# Provider history

This is the journal of what actually happened while wiring up and rehearsing
the three real LLM providers (`gemini`, `groq`, `vertex`), plus a fourth that
was live for a while and one that was researched and rejected. None of this
is required reading to set the project up — for that, see [Step 4: Configure
an LLM provider](../setup/04-llm-provider.md) and [Switching providers and
API keys](../operations/overrides.md). It is kept and published because it is
real evidence of what was tried, what broke, and what it cost — not a
theoretical description.

## Gemini (AI Studio): the account-level block

Gemini was the first provider brought up, with a free AI-Studio key
(`GEMINI_API_KEY`, `LLM_PROVIDER=gemini`). Live verification then started
failing: `403 PERMISSION_DENIED — Your project has been denied access` on
newer flash models, persistent `429` on older ones.

Per Google's own AI Developer Forum, this is an **automated Trust & Safety
account flag** — Google staff confirmed in one thread that "a flag has been
placed on your account." It is not a model-naming, project, or code issue.
One documented trigger: hitting repeated `429`s / testing many models
back-to-back without backoff — which is exactly what happened here during
troubleshooting. That incident is why this project's testing-hygiene rule
exists at all (see `CLAUDE.md`'s "LLM API testing hygiene" section): one
deliberate live call per real verification need, never a burst across models
or keys to "see what sticks."

The block was confirmed exhausted as of 2026-07-23: a second API key under a
different Google project hit the same `403`; keys under multiple genuinely
different Google accounts were blocked too. Per the forum, the only
documented fix is attaching GCP billing — a trade-off this project
deliberately avoids (see `CLAUDE.md`'s "Substitutions from the brief").
Cross-vendor provider-agnosticism was demonstrated via Groq instead.

**Resolved, 2026-08-10:** the API key was updated (new key, same or a
different Google account — not investigated further), and
`scripts/manual_verify_step4.py` succeeded live: real structured output,
non-zero token usage, no `403`. Whatever specifically tripped the flag on the
earlier key was never reproduced, and — per the testing-hygiene rule — was
not chased with a root-cause investigation; that would just be more burst
testing against a provider that had already shown it could flag an account.

**This resolution does not make the risk hypothetical.** The fix landing
once doesn't mean the flag can't recur, and the discipline that avoids
triggering it (one deliberate call, mocked/cassette tests for exploration,
stop-and-diagnose instead of retry-with-a-different-key on `403`/`429`)
still applies going forward, for Gemini and for every other provider's free
tier. Groq remains the provider used for the live demo regardless of
Gemini's resolution — that choice didn't change just because Gemini became
usable again.

One more oddity from this era, noted but not chased further: during the
build-step-7 provider-swap demo (`scripts/demo_provider_swap.py`), an
isolated direct call to `GeminiProvider` consistently reproduced the
documented `403`, but running through the full `orchestrator.run_review()`
pipeline (real PR diff content as the prompt) once produced a different
error instead — `401 ACCESS_TOKEN_TYPE_UNSUPPORTED`. It was not reproduced
after several isolated retries (concurrency, sequencing after Groq, matching
model/config all ruled out). Likely just another inconsistent error shape
from the same flagged-account block depending on request specifics — not
chased further, to avoid more burst-testing against a blocked provider. The
demo still proved what it set out to: `LLM_PROVIDER` is a true runtime seam
(no server restart needed to swap it), and the resilience guarantee holds
even under total provider failure — every specialist's never-raise contract
caught the real Gemini error and the orchestrator still posted a coherent
comment with three visible failed rows, no crash.

## Vertex: two bugs found by real live calls

Vertex AI was in the original plan, then removed when its billing-account
requirement collided with this project's no-card constraint (the adapter was
implemented, then pulled — see `CLAUDE.md`'s "Substitutions from the
brief"). It came back on 2026-08-14 once GCP billing/ADC access became
available, as a real, code-complete third provider — matching `SPEC.md`'s
stated default. Its credential is a GCP service-account identity, not an API
key string, resolved by `providers/vertex_credentials.py`.

**Bug 1 — missing OAuth scope.** The first live run of
`scripts/manual_verify_vertex.py` against a real GCP service-account
credential got past credential resolution and project-id derivation cleanly
(a real project id was resolved, no credential material was ever printed),
and the call reached Google's real OAuth token endpoint — a genuine network
round-trip, proving the whole path (credential resolution → `VertexProvider`
construction → the `google-genai` `vertexai=True` client → an actual HTTPS
call) was wired correctly end-to-end. The call itself then failed with
`google.auth.exceptions.RefreshError: invalid_scope: Invalid OAuth scope or
ID token audience provided`. Root cause: `VertexProvider` was constructing
its service-account credentials without the required `cloud-platform` OAuth
scope (`providers/google_genai.py`) — the implicit-ADC path already had
the correct scope via `google-genai`'s own SDK, but the explicit
service-account path (the hosted/Render production configuration) did not.
Per the testing-hygiene rule, this was **not** retried with a different
scope or key while diagnosing it; one deliberate follow-up call was made
once the fix had landed, and it confirmed the scope fix worked — credential
resolution, project derivation, and OAuth token refresh all succeeded, a
genuine round-trip against Google's real infrastructure, not a mock.

**Bug 2 — the shared model default doesn't exist on Vertex's catalog.**
That same follow-up call reached Vertex AI's real `generateContent` endpoint
and failed with a different, unrelated error: `404 NOT_FOUND: Publisher
model 'projects/tovtech-vertex-imagen/locations/us-central1/publishers/
google/models/gemini-flash-latest' was not found or your project does not
have access to it.` Vertex's publisher-model catalog uses its own model ids
(often dated, e.g. `gemini-2.0-flash-001`-style) that don't necessarily
mirror AI-Studio's aliases, so `gemini-flash-latest` — this project's shared
default — doesn't resolve as a Vertex publisher model for this
project/region.

Rather than guessing model IDs via repeated `generateContent` calls,
candidate model IDs were checked via lightweight, no-cost
`GET https://us-central1-aiplatform.googleapis.com/v1/publishers/google/models/{model}`
catalog-existence requests first — metadata reads, not generation calls.
Checking several of these in one pass is not the "bursting live calls"
pattern the testing-hygiene rule targets, since there's no token cost and no
completion request involved; it's the right way to narrow configuration
*before* the one deliberate generation call. Result: `gemini-2.0-flash-001`,
`gemini-2.0-flash-lite-001`, `gemini-1.5-flash-002`, and
`gemini-flash-latest` all 404'd; `gemini-2.5-flash` and
`gemini-2.5-flash-lite` both existed — this project's Vertex catalog only
carries the 2.5 generation. One deliberate `generateContent` call was then
made with `LLM_MODEL=gemini-2.5-flash`: full success — a valid
structured-output response with non-zero token usage
(`Greeting(message='Hello there!')`, 20 tokens in / 8 out), the first
genuinely complete end-to-end live verification of this provider. A
`("vertex", "gemini-2.5-flash")` pricing entry was added to
`providers/pricing.py` to match.

Vertex has since been split onto its own `VERTEX_MODEL` env var (default
`gemini-2.5-flash`, the confirmed-working value) rather than sharing
`LLM_MODEL` with gemini, so an operator enabling vertex gets a working model
with no override needed.

## GitHub Models: the second cross-vendor provider (and its real retirement)

At the point a second genuinely-live cross-vendor provider was wanted (to
demonstrate alongside Groq at showcase time — Gemini was not expected to
come back at the time this decision was made), two other free-tier options
were researched and ruled out first:

- **Cerebras** — despite older blog posts describing a perpetual free RPM
  quota, the account's actual current policy (confirmed live) is a "$5 free
  credit" that still requires billing info attached: every available model
  (`gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b`) returned `402 Payment
  Required`. Same dealbreaker as Vertex's original no-card constraint.
- **Mistral** — not attempted. Reported free-tier RPM as low as 1
  request/min (unconfirmed exact number — Mistral stopped publishing
  free-tier limits publicly), which would seriously risk the 15-second
  target given this project's 3-concurrent-calls-per-review pattern. Cohere
  was also considered (20 RPM, no card) but needs a new separate
  account/signup, explicitly trial-only.

**GitHub Models was chosen** — it rides an existing GitHub account (a
fine-grained PAT with the "Models: read" permission), so no new account and
no new account-flagging risk. It exposes an OpenAI-compatible API
(`https://models.github.ai/inference`) with real OpenAI models
(`openai/gpt-4o-mini`, `GITHUB_MODELS_MODEL`) — a genuinely different vendor
*and* model family from both Gemini (Google) and Groq (Llama), the strongest
cross-vendor story among the providers actually usable here. Its known
caveat, flagged but not addressed: free-tier rate limits are modest
(single-digit RPM / ~150 requests per day on low-access models) — fine for a
demo, a real constraint at any sustained volume.

**A real bug was caught by live testing** (in the now-removed
`providers/github_models.py`, deleted when this provider was retired below):
OpenAI's strict `json_schema` mode requires `"additionalProperties": false`
explicitly present on **every** object schema, including nested `$defs`
entries — Pydantic's `model_json_schema()` doesn't set this anywhere by
default. A flat test schema surfaced the top-level case first (a live
`400`); the real nested container schemas this project actually uses (e.g.
`SecurityFindings` wrapping `SecurityFinding` via `$defs`) then surfaced that
a top-level-only fix wasn't enough (another live `400`, a different nested
path). It was fixed with a generic recursive walker
(`_add_additional_properties_false`) rather than special-casing `$defs`, so
any nesting shape Pydantic produces is covered — and both cases are locked
in by tests, not just fixed ad hoc.

It was live-verified end-to-end: a single-schema call via the now-removed
`scripts/manual_verify_github_models.py`, then the real nested
`SecurityFindings` schema directly, then a full 3-specialist
`orchestrator.run_review()` run against PR #3 — 7.5 seconds, all three
specialists succeeded with real findings, comment posted and independently
confirmed via `gh api`.

**GitHub Models was then genuinely retired**, on 2026-07-30 — not a
simulated failure. A redeploy onto `LLM_PROVIDER=github_models` made all
three specialists fail visibly, then a redeploy back onto `groq` recovered,
with the review ticket surviving both restarts intact. It is the reason
`groq` — not `github_models` — is the provider actually left configured
today.

## Current state

**Groq is the primary live provider** (`LLM_PROVIDER=groq`,
`llama-3.3-70b-versatile`), pulled forward from a later build step
specifically to have a working live path from early on. Free tier, no card.
Structured output uses `json_object` mode plus a schema-instructing system
prompt, since this model doesn't support Groq's `json_schema` constrained
decoding — verified live.

Gemini and Vertex are both live and fully verified as described above, but
neither is the provider used for the live demo — that choice was made for
the reasons given in each section, not because either provider stopped
working.
