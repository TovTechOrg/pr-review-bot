# Vertex model selection must probe entitlement, not trust the catalog listing

## 1. Background

Production's Vertex provider failed every review with the same error, once per
specialist:

```
RuntimeError: all specialists failed: Security: 404 NOT_FOUND. {'error': {'code': 404,
'message': 'Publisher model `projects/tovtech-vertex-imagen/locations/us-central1/
publishers/google/models/gemini-3.1-flash-lite` was not found or your project does not
have access to it. ...'}}
```

`slot_config` for `(provider='vertex', slot_index=0)` held
`model='gemini-3.1-flash-lite'`, `project='tovtech-vertex-imagen'`,
`location='us-central1'`. The credential and project were fine: a live,
credentialed `providers.catalog.list_vertex_models` call against that exact
pair succeeded *and listed `gemini-3.1-flash-lite` as an entry*.

The reason both facts are true at once: **Vertex AI's `client.models.list()` is
not scoped by per-project entitlement.** It returns essentially the global
Model Garden catalog -- the same live listing also returned `veo-*`, `lyria-*`,
`gemini-3.8-flash`, `medgemma` and other entries this project plainly cannot
call. Entitlement is enforced only when a request actually names the publisher
model. "It appears in the list" proves the credential authenticates. It proves
nothing about whether this project may generate with that model.

Two codebases independently built a model picker on top of that listing and
made the same wrong inference:

1. **pr-review-bot** (this repo): `providers/catalog.py::_list_generative_models`,
   consumed by `dashboard/environment.py`'s `_validate_model_var`,
   `_validate_vertex_credential` and `_fetch_models_for_provider`.
   `_validate_model_var`'s entire gate is `candidate not in (result.models or [])`
   (`dashboard/environment.py:131`).
2. **TovTechOrg/onboarding-wizard**: `llm_client.py::_list_generative_models` /
   `list_vertex_models`, which populates the model dropdown whose value is
   written into `slot_config` at provisioning time.

Both carry near-identical code and near-identical docstring reasoning. The
wizard's version states it outright: Vertex responses never populate
`Model.supported_actions`, so filtering on it would "silently empt[y] the
entire Vertex catalog regardless of credential", and every model is therefore
let through unfiltered.

That reasoning is correct as far as it goes. The unstated corollary -- *so
anything in the unfiltered list is safe to offer as a real choice* -- is false,
and it is what let a non-functional model reach production through a flow that
reported it as validated.

### 1a. A cheap entitlement probe exists (verified live, 2026-09-11)

`client.models.count_tokens()` in Vertex mode issues
`POST .../publishers/google/models/<model>:countTokens`. It is free, generates
nothing, and must resolve the publisher model exactly as `generateContent`
does. Two deliberate live calls against the production project confirmed it
discriminates:

```
gemini-3.1-flash-lite: countTokens FAILED status=404
    'Publisher model .../gemini-3.1-flash-lite was not found or your project
     does not have access to it. ...'
gemini-2.5-flash:      countTokens OK total_tokens=1
```

The 404 is byte-for-byte the failure production hit at review time. This is the
single fact the rest of this design rests on: the listing endpoint is provably
the wrong gate, and `countTokens` is a correct one.

It also sits inside root `CLAUDE.md`'s LLM-hygiene carve-out. The
"one deliberate live call" limit governs generation/completion requests;
`countTokens` is a free metadata call, the same class as the listing calls
`catalog.py` already makes.

### 1b. A second, independent defect: no write path validates at all

While scoping the above: `dashboard/environment.py`'s two `slot_config`
writers -- `_apply_llm_credential` (guided setup, :378) and
`_apply_slot_config_patch` (config panel, :904) -- hand their `model` argument
straight to `store.set_slot_config` with no model check of any kind.
`POST /api/environment/validate/{var}` is a *separate endpoint the frontend
chooses to call first*. The gate is a frontend courtesy; any non-UI request,
or a UI race between validate and save, writes an unvalidated model and reports
success.

So fixing `_validate_model_var` alone would fix the wrong half. This is the
failure shape root `CLAUDE.md` already records for
`set_cooldown_override`/`set_usage_cap_override`: a validation gap that exists
because checking lives with one caller instead of in one predicate every writer
calls. The fix follows the resolution that section prescribes --
`cooldown_config.problems()`'s shape, not prose about who calls whom.

## 2. Goal

No model reaches a `slot_config` row, through any path in either repo, without
having passed a live entitlement probe against the same `(project, location)`
it will run under -- and a probe that *could not run* is never reported as a
model that does not work.

## 3. Decisions taken (and what was rejected)

| Decision | Chosen | Rejected, and why |
|---|---|---|
| Probe scope | Probe the **selected candidate** at validate/save time -- exactly one extra live call per save | Probing every listed model at list time: dozens of concurrent calls per page load, which is the burst pattern `CLAUDE.md`'s hygiene rule exists to prevent even though `countTokens` is free. Eagerly probing a curated shortlist: two code paths plus a hand-maintained list |
| Probe unavailable (429/5xx/timeout) | **Block the write**, with its own retryable error code | Fail-open with a "saved unverified" warning: nothing re-checks that state later (section 8), so "saved unverified" silently becomes "saved, broken" -- the exact hole being closed |
| Retroactive re-validation | **None.** Existing rows are left alone | A non-blocking boot probe, a hard boot gate, and an on-demand dashboard re-probe were all weighed and declined (section 8 records the accepted risk) |
| Cross-repo mechanism | **Independent parallel fix in each repo**, with reciprocal docstring references | Extending `contracts/provisioning.json` with a model-validation clause (would have bumped `contract_version` to 2) |
| Provider scope | **Vertex and Gemini** (both expose a free `countTokens`); **Groq unprobed** | Probing Groq means a real, paid chat completion on every model save -- straight into the abuse-flag pattern `CLAUDE.md` forbids |
| `scripts/set_override.py --model` | **Probes and refuses**, with a `--skip-probe` escape hatch | Leaving the CLI unprobed keeps the incident reproducible through the very tool used to repair production |

## 4. `providers/catalog.py` -- the probe

Two new functions beside the existing listing calls, same synchronous style,
same `_LIST_TIMEOUT_MS`, same `CatalogResult` return type (`models` is always
`None` for a probe; only `ok`/`error` carry meaning):

```python
def probe_vertex_model(
    service_account_info: dict | None,
    model: str,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult: ...

def probe_gemini_model(api_key: str, model: str) -> CatalogResult: ...
```

Each builds the same client its listing counterpart builds (`probe_vertex_model`
reusing `list_vertex_models`'s project/location resolution verbatim, including
the `DEFAULT_VERTEX_LOCATION` literal fallback and its reason), then calls
`client.models.count_tokens(model=model, contents="ping")`.

**Error classification is the load-bearing part.** Two new codes:

- `model_not_callable` -- and *only* for a 404/`NOT_FOUND` on the probe itself.
  This is the one code that means "this model is not usable here."
- `model_probe_unavailable` -- the probe could not reach a verdict: 429, 5xx,
  timeout, transport failure.

Everything else keeps its existing classification: `unauthorized` / `forbidden`
for credential problems (via `_classify_vertex_auth_exception` then
`_classify_exception`), `invalid_service_account_json` for malformed key
material. Collapsing `model_probe_unavailable` into `model_not_callable` would
merely relocate the original bug -- a rate-limited probe would condemn a working
model and send an operator hunting for a replacement that was never needed.

`_list_generative_models`'s docstring gains the finding from section 1: the
Vertex listing is the global Model Garden, membership in it is not entitlement,
and `probe_vertex_model` is what answers the entitlement question. The existing
`supported_actions` reasoning stays -- it was never wrong, only incomplete.

## 5. `providers/model_check.py` -- one predicate, every writer

A new one-purpose module, modelled on `review_queue/cooldown_config.py`:

```python
def problems(
    provider: str,
    slot: int,
    model: str,
    vertex_gcp_project: str | None = None,
    vertex_gcp_location: str | None = None,
    credential: str | dict | None = None,
) -> list[str]: ...
```

Returns an empty list when the model is callable, otherwise the structural
error codes above. For `provider == "groq"` it returns `[]` unconditionally,
with a docstring stating why (no free token-counting endpoint; Groq's own
listing *is* key-scoped, unlike Vertex's).

`credential` is the one parameter that is not obvious, and it exists for a
concrete case rather than for generality. Left `None` -- the config panel, the
CLI, any re-validate of an already-configured slot -- the credential is
resolved from the slot itself, via `providers.credentials.resolve` for
gemini/groq and `providers.vertex_credentials.resolve_service_account_info` for
vertex, so those callers all gate through one resolution path instead of each
assembling its own.

Passed explicitly, it is probed instead of the slot's. **Guided setup requires
this**: `_apply_llm_credential` receives a freshly-uploaded credential in its
own request body and must probe *before* pushing it to Render (section 5a's
ordering rule), so at probe time that credential exists nowhere the slot could
resolve it from. Resolving from the slot there would silently probe the
*previous* credential in that slot -- or none at all on a first-time setup --
and answer a question nobody asked. It takes the shape each family already
uses: a raw API-key string for gemini, the decoded service-account `dict` for
vertex.

It lives in `providers/` rather than `dashboard/` because `scripts/set_override.py`
is one of its callers and `dashboard/` must not become a dependency of the
CLI.

### 5a. Callers

| Caller | Today | After |
|---|---|---|
| `dashboard/environment.py::_validate_model_var` | `candidate not in (result.models or [])` | listing check **and** `model_check.problems(...)`; a probe failure returns its own code with `models` still populated so the dropdown survives |
| `dashboard/environment.py::_apply_llm_credential` | no model check | `problems(...)` before `store.set_slot_config`; non-empty -> `{"applied": [...], "failed": [{"key": slot_config_key, "error": <code>}]}` |
| `dashboard/environment.py::_apply_slot_config_patch` | no model check | same, before `store.set_slot_config` |
| `scripts/set_override.py --model` | pricing warn only | refuses on a non-empty `problems(...)`, unless `--skip-probe` |

Both dashboard refusals sit exactly where each function's existing
`vertex_gcp_location_required` guard already sits -- same position, same
precedent, same principle: refuse before the write, not after a PR arrives.

`_apply_llm_credential` needs one ordering note: its probe must run **before**
the Render credential push, not between the push and the DB write, so a refused
model cannot leave a pushed credential with no matching `slot_config` row. It
passes its request body's own credential through `problems(..., credential=...)`
for the reason given in section 5.

One consequence worth stating so it is not mistaken for an oversight: in guided
setup, a brand-new credential's model choice is probed at **Apply** time, not
while the operator is picking from the dropdown. `_validate_model_var` (which
backs interactive validation) reads the stored slot, and during first-time
setup there is nothing stored yet. The dropdown therefore shows the unverified
listing, and Apply is where the verdict lands -- which is exactly why the gate
had to move into the write path rather than stay in the validate endpoint.

### 5b. `set_override.py`'s two different rules

The CLI already warns without refusing when a model has no `providers/pricing.py`
rate entry. That stays, and the new refusal does not contradict it, because the
two conditions differ in kind:

- **Unpriced** -> warn. The model runs fine; the PR comment simply shows no cost
  estimate (design spec 2026-08-18 sections 6a/6b).
- **Not callable** -> refuse. Every review under it 404s.

`--skip-probe` exists for the offline/no-credential case (and for a
break-glass repair when a provider is unreachable but the operator knows the
model is good). It prints what it skipped; it is never the default.

## 6. `dashboard/static/dashboard.html`

The model dropdown stops implying verification. Listed-but-unprobed models are
labelled as listed by Vertex and verified on save, and the two new codes join
the existing error-string map beside `no_credential_configured`:

- `model_not_callable` -> "Vertex lists this model globally, but project *X*
  isn't entitled to call it. Pick a different model."
- `model_probe_unavailable` -> "Couldn't verify this model right now (provider
  unreachable). Try again."

Per root `CLAUDE.md`, the `ui-visual-review` skill runs against this change
before it is called done.

## 7. onboarding-wizard changes (parallel, same-shaped)

1. `llm_client.py` gains `probe_vertex_model` / `probe_gemini_model` with the
   same two error codes and the same "a probe that could not run is not a
   failed model" distinction. Its async style is preserved
   (`client.aio.models.count_tokens`).
2. `router.py::_seed_provider_config` -- the single choke point where a
   visitor's chosen model becomes a `slot_config` row -- refuses to write when
   the probe does not pass, and reports that to the visitor the same way it
   already reports a Render push failure or `slot_config_seed_failed`. The
   probe runs before the Render credential push, for the same
   ordering reason as section 5a.
3. `static/index.html` gains the two error strings **in both locales** -- the
   file carries parallel English and Hebrew dictionaries (see
   `err_llm_no_models_available` at :1311 and :1464); a string added to one
   only is a bug in the other.
4. `_list_generative_models`'s docstring there gets the same correction as
   this repo's.

### 7a. The known weakness of doing this twice

Each repo's probe function carries a docstring naming its counterpart in the
other repo and this spec. That is a convention, not a mechanism. The
alternative considered and declined was publishing the rule in
`contracts/provisioning.json`, which would have bound the wizard the way the
column schema already binds it. Stated plainly so a future reader does not have
to rediscover it: **nothing mechanically prevents these two implementations
from diverging again, and a shared unstated assumption diverging into two
copies is precisely how this bug came to be written twice.** If a third
consumer ever appears, promote the rule into the contract.

## 8. Explicitly out of scope

- **Retroactive re-validation of existing `slot_config` rows.** Every row ever
  written went through the unvalidated flow, so any already-provisioned
  deployment holding an unlucky model keeps 404ing until someone edits it.
  Accepted knowingly: a non-blocking boot probe, a hard boot gate (rejected
  additionally because the dashboard that repairs the model is served by the
  same process a boot gate would stop), and an on-demand dashboard re-probe
  were all weighed and declined.
- **Groq probing** -- section 3.
- **`contracts/provisioning.json`** -- unchanged; `contract_version` stays 1.
- **The production unblock.** Restoring `slot_config` to `gemini-2.5-flash`
  (confirmed both listed and callable in section 1a, and this repo's own
  `config.py` default) is a separate, already-diagnosed fix tracked
  independently of this design.

## 9. Testing

All mocked -- no live calls in the suite, per `CLAUDE.md`'s hygiene rule. The
section 1a spike was the one deliberate live verification this design needed.

- `probe_vertex_model` / `probe_gemini_model`: a 404 from `count_tokens` ->
  `model_not_callable`; 429, 500 and timeout -> `model_probe_unavailable` and
  **never** `model_not_callable`; a `google.auth` `RefreshError` -> `unauthorized`;
  malformed key material -> `invalid_service_account_json`.
- `model_check.problems`: empty list on a passing probe; the probe's own code
  otherwise; unconditionally empty for groq.
- One test per write path asserting `store.set_slot_config` is **not reached**
  when `problems(...)` is non-empty -- `_apply_llm_credential`,
  `_apply_slot_config_patch`, and `set_override.py`.
- `problems(credential=...)` probes the passed credential and never resolves
  the slot's -- asserted by making slot resolution raise, so a regression that
  reintroduces the resolve cannot pass silently.
- `_apply_llm_credential` passes its request-body credential through, rather
  than the slot's.
- `_apply_llm_credential` refuses before `render_client.push_env_var` is called
  (the ordering guarantee in section 5a).
- `set_override.py --skip-probe` writes without probing and says so.
- `_validate_model_var` returns `model_not_callable` for a listed-but-unusable
  model while still returning the populated `models` list.
- Wizard: the same probe-classification tests, plus `_seed_provider_config`
  writing nothing when the probe fails, plus a test that both locale
  dictionaries carry the new keys.
