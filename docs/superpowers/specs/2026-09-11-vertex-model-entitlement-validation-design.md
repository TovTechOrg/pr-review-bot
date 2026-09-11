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

No model reaches a `slot_config` row, and no `slot_config` row becomes the
active one, through any path in either repo, without a live entitlement probe
having passed against the same credential, project and location it will run
under -- and a probe that *could not run* is never reported as a model that
does not work.

## 3. Decisions taken (and what was rejected)

| Decision | Chosen | Rejected, and why |
|---|---|---|
| Probe scope | Probe the **selected candidate** at validate/save time -- exactly one extra live call per save | Probing every listed model at list time: dozens of concurrent calls per page load, which is the burst pattern `CLAUDE.md`'s hygiene rule exists to prevent even though `countTokens` is free. Eagerly probing a curated shortlist: two code paths plus a hand-maintained list |
| Probe unavailable (429/5xx/timeout) | **Block the write**, with its own retryable error code | Fail-open with a "saved unverified" warning: nothing re-checks that state later (section 8), so "saved unverified" silently becomes "saved, broken" -- the exact hole being closed |
| Retroactive re-validation | **None.** Existing rows are left alone | A non-blocking boot probe, a hard boot gate, and an on-demand dashboard re-probe were all weighed and declined (section 8 records the accepted risk) |
| Cross-repo mechanism | **`contracts/provisioning.json` gains a `model_validation` block** (`contract_version` 1 -> 2), vendored into the wizard, plus a wizard-side conformance test | Independent parallel fixes with reciprocal docstring references: first choice, reversed on review. Two docstrings pointing at each other is exactly what was in place while this bug was being written twice |
| Arming a slot | **Probing gates activation too** -- a changed `provider` or `key_index[p]` probes the newly-active pair | Leaving activation ungated (an unguarded route to arming an unusable model); probing every submitted `key_index` regardless of change (up to 3 live calls on an unrelated cooldown edit) |
| Frontend row gate | **The model select joins the row's validate gate** | Server-side gate only: correct but user-hostile -- a green Validate followed by a rejected Save |
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
| `dashboard/environment.py::_apply_render_patch` | routes `_DIRECT_EDIT_VARS` sets through `_validate_var` | inherits the probe unchanged -- but must stop flattening the verdict (see below) |
| `dashboard/environment.py::_apply_config_patch` | no model check on `provider` / `key_index` | probes the newly-active pair when arming changes (section 5c) |
| `scripts/set_override.py --model` | pricing warn only | refuses on a non-empty `problems(...)`, unless `--skip-probe` |
| `scripts/set_override.py` activation | no model check | same arming rule as 5c |

`_apply_render_patch` needs one small fix beyond inheriting the predicate: it
collapses every verdict to `failed_validation` (`dashboard/environment.py:543`),
so `model_not_callable` and `model_probe_unavailable` would never reach the UI
through that path and section 6's strings would be dead code there. It must
propagate `check["error"]` instead.

The two `slot_config` refusals -- `_apply_llm_credential`'s and
`_apply_slot_config_patch`'s -- sit exactly where each function's existing
`vertex_gcp_location_required` guard already sits: same position, same
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

### 5c. Arming a slot is a write too

Neither `provider` nor `key_index` carries a model value, so neither looks like
a model write -- but both change *which* `slot_config` row is live, and slot N's
credential, project and location all differ from slot 0's. A model that probed
clean in slot 0 is a genuinely open question in slot 1. Flipping either is
therefore a third route to "the UI reported `applied`, every subsequent review
404s", and post-fix the most likely one.

`_apply_config_patch` probes on arming, scoped by what actually changed:

- Compare each submitted `provider` / `key_index[p]` against its stored value.
  Unchanged -> no probe. This matters because `saveConfig()` sends `key_index`
  for **every** provider on every save, unconditionally and by design
  (`dashboard.html:2988`), so an unscoped rule would fire up to three live calls
  every time someone edits a cooldown value.
- Probe only the pair that becomes active: the target slot's stored model,
  under the target slot's own credential/project/location. At most two probes
  (a `provider` change and a `key_index` change can arrive together).
- A failed probe fails **that field alone** in the existing `applied`/`failed`
  response. `_apply_config_patch` already applies each group independently, so
  the cooldown or tuning changes riding in the same PATCH still land.

`scripts/set_override.py` follows the same rule: it activates by default, so its
activation path probes the target slot, honouring `--skip-probe` identically.

This has a second effect worth naming, because it partially recovers something
section 8 declines. Arm-time probing is a **retroactive backstop**: a row
written before this fix -- and every existing row was -- is caught at the exact
moment it would start causing 404s, with no boot probe, no background sweep,
and no cost to a deployment that never touches the setting. It does not find a
bad row in a slot that is *already* armed and left alone, which is why section 8
still records that gap rather than claiming it closed.

## 6. `dashboard/static/dashboard.html`

The model dropdown stops implying verification. Listed-but-unprobed models are
labelled as listed by Vertex and verified on save, and the two new codes join
the existing error-string map beside `no_credential_configured`:

- `model_not_callable` -> "Vertex lists this model globally, but project *X*
  isn't entitled to call it. Pick a different model."
- `model_probe_unavailable` -> "Couldn't verify this model right now (provider
  unreachable). Try again."

### 6a. The model select must join the row's validate gate

The per-slot editor already tracks, per row, whether the current
**project+location** pair has been live-confirmed this page load
(`rowVertexValidated`, `rowVertexBaseline`), and disables Save until a changed
pair is re-validated. The model select is wired into neither structure, and
`validateSlotConfigRow` calls `/credential/{provider}/models`, which checks
credential+project+location and never the selected model. **There is no path
through this UI on which the chosen model is checked against anything** -- which
is how the incident's value was saved from an otherwise-valid row.

So: the model joins the row's recorded baseline and re-arms its validated flag
on change exactly as the other two fields do, and `validateSlotConfigRow` probes
the selected model as part of its verdict. The server-side predicate stays the
real gate; this stops the UI claiming a verdict it never obtained.

That state is currently vertex-only (`rowVertexValidated`/`rowVertexBaseline`),
and only vertex rows carry a Validate button -- until now there was nothing
per-row for a gemini or groq row to validate. Since gemini is probed too, its
rows gain the same Validate control and the same gate, and the two row-state
maps lose their `vertex` prefix along with their vertex-only assumption. Groq
rows keep no gate, because there is nothing to check (section 5's
`problems()` returns `[]` for groq); their model select stays a plain dropdown.
Leaving gemini out would hand it the exact "green Validate, rejected Save"
experience this section exists to prevent.

Per root `CLAUDE.md`, the `ui-visual-review` skill runs against this change
before it is called done.

## 7. The contract carries the rule; the wizard implements it

### 7a. Why this is in the contract rather than in two docstrings

The first version of this design fixed each repo independently and had them
reference each other in docstrings. That was reconsidered, and reversed, for a
plain reason: two implementations with near-identical docstrings is precisely
what was in place *while this bug was being written twice*. The mechanism that
failed cannot be the mechanism that prevents the recurrence.

What moving the rule into `contracts/provisioning.json` actually buys:

1. `ci.yml`'s `docs` job regenerates the contract and byte-compares it, so a
   change to the rule here that is not regenerated in the same commit turns CI
   red. Local, no sibling checkout, no network -- the producer is never blocked
   on the consumer, exactly as `CLAUDE.md`'s cross-repo section requires.
2. `consumer-contract-lag.yml` reports a stale vendored copy daily, advisory
   only. Divergence becomes visible instead of silent.
3. The consumer can assert its own conformance against the vendored file
   (section 7c).

And the ceiling, stated so nobody over-trusts it: **the contract cannot force
the wizard to call a probe before writing.** It declares the rule and makes
drift visible; section 7c's test is what turns that into enforcement, and that
test lives in the consumer. This is cooperative, not compile-time. It is
strictly more than a docstring, and less than a shared module -- which the two
repos cannot have, having no shared dependency.

### 7b. The `model_validation` block

A new top-level block, generated by `scripts/gen_contract.py` from this repo's
own constants like every other block, carrying per-provider:

- whether a probe is **required before a model is written** into `slot_config`,
- the probe **mechanism** (`count_tokens`), and
- the canonical **error codes** (`model_not_callable`, `model_probe_unavailable`).

Groq appears with no probe and a stated reason, rather than being omitted --
an absent entry reads as an oversight, an explicit `null` reads as a decision.

The error codes belong here despite being reporting vocabulary rather than row
shape. The wizard inventing `model_unavailable` while this repo says
`model_not_callable` is the same divergence this block exists to prevent,
wearing a different costume.

A new block is a **shape** change, so `contract_version` goes 1 -> 2, per
`gen_contract.py`'s own stated rule. The block carries provider names, a
mechanism name and error-code strings -- no secret has a shape it could occupy,
consistent with the generator's existing constraint.

**This widens what the contract is**, from the shape of the row to the shape
*and validity* of the row. That is a deliberate extension of its charter, and it
makes root `CLAUDE.md`'s "carries names, placements, SQL types and non-secret
operational defaults only" inaccurate as written. Updating that paragraph is
part of this work, not a follow-up -- a contract whose own governing document
misdescribes it is how the next reader concludes the block does not belong.

### 7c. onboarding-wizard changes

1. `llm_client.py` gains `probe_vertex_model` / `probe_gemini_model` with the
   contract's error codes and the same "a probe that could not run is not a
   failed model" distinction. Its async style is preserved
   (`client.aio.models.count_tokens`).
2. `router.py::_seed_provider_config` -- the single choke point where a
   visitor's chosen model becomes a `slot_config` row -- refuses to write when
   the probe does not pass, reporting it the way it already reports
   `slot_config_seed_failed`. The probe runs before the Render credential push,
   for the same ordering reason as section 5a.
3. **A conformance test reading the vendored `contracts/provisioning.json`**:
   for every provider the contract marks as requiring a probe, the wizard
   implements one; for every provider it marks as not requiring one, the wizard
   does not invent a probe; and the wizard's reported error codes are exactly
   the declared set. This is the enforcement -- without it the wizard could
   vendor the new contract, ignore its content, and pass every check.
4. `static/index.html` gains the error strings **in both locales** -- the file
   carries parallel English and Hebrew dictionaries (see
   `err_llm_no_models_available` at :1311 and :1464); a string added to one only
   is a bug in the other.
5. `_list_generative_models`'s docstring there gets the same correction as this
   repo's.

Per `CLAUDE.md`'s ownership direction, this repo's half lands green on its own
and the wizard catches up in a following commit. The lag job reports the
interval; nothing here blocks on the consumer.

## 8. Explicitly out of scope

- **Systematic retroactive re-validation of existing `slot_config` rows.**
  Every row ever written went through the unvalidated flow. Section 5c's
  arm-time probe recovers part of this for free -- a stale bad row is caught the
  moment anyone arms it -- but a row that is *already* armed and never touched
  again is still only discovered by a failing review. Accepted knowingly: a
  non-blocking boot probe, a hard boot gate (rejected additionally because the
  dashboard that repairs the model is served by the same process a boot gate
  would stop), and an on-demand dashboard re-probe were all weighed and
  declined.
- **Groq probing** -- section 3.
- **The config panel's silent discard of unsaved edits.** `saveSlotConfig`
  ends with `fetchEnvironmentConfig()`, which rebuilds every row from server
  state, destroying pending edits in every *other* row without warning
  (`dashboard.html:2585`, and the same shape as the data-loss path already fixed
  at :2846). A real defect, but an independent one -- it would exist with or
  without the entitlement bug, and a dirty-state/rerender fix is a different
  change than this one. Logged in `ISSUES.md`'s Parked Issues.
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
- `_apply_render_patch` propagates `model_not_callable` /
  `model_probe_unavailable` rather than flattening both to
  `failed_validation`.
- Arming (section 5c): an unchanged `provider`/`key_index` triggers **no** probe
  (asserted by making the probe raise, so a regression that over-probes cannot
  pass); a changed one probes the target slot's stored model under that slot's
  own credential; a failed arming probe fails that field alone while the
  cooldown/tuning fields in the same PATCH still apply.
- `scripts/gen_contract.py` emits the `model_validation` block deterministically
  with `contract_version == 2`, and `tests/test_provisioning_contract.py`
  pins its shape and the byte-compare.
- Frontend (section 6a): changing the model select re-arms the row's validate
  gate and disables Save, for vertex and gemini rows alike;
  `validateSlotConfigRow`'s verdict includes the model; a groq row has no gate
  and its Save stays enabled.
- Wizard: the same probe-classification tests, plus `_seed_provider_config`
  writing nothing when the probe fails, plus the section 7c conformance test
  against the vendored contract, plus a test that both locale dictionaries carry
  the new keys.
