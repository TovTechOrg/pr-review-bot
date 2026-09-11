# Vertex Model Entitlement Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No model reaches a `slot_config` row, and no `slot_config` row becomes the active one, without a live `count_tokens` entitlement probe having passed against the same credential, project and location it will run under.

**Architecture:** A probe pair in `providers/catalog.py` (`probe_vertex_model` / `probe_gemini_model`) wraps `client.models.count_tokens`, which reproduces `generateContent`'s exact 404 for free. One shared predicate, `providers/model_check.problems()`, resolves a slot's credential and calls the probe; every writer -- two dashboard save paths, the arming path, and the CLI -- gates on that one predicate rather than each checking for itself. The rule is published in `contracts/provisioning.json` so the onboarding-wizard implements the same gate against a vendored declaration instead of a copied docstring.

**Tech Stack:** Python 3.12, `google-genai`, FastAPI, pydantic v2, pytest, `uv`, vanilla JS (no framework) in `dashboard/static/dashboard.html`.

**Spec:** `docs/superpowers/specs/2026-09-11-vertex-model-entitlement-validation-design.md` -- read it before Task 1. Every task argues from it.

## Global Constraints

- **No live network calls in tests, ever.** Root `CLAUDE.md`'s LLM API testing hygiene section. Every probe test mocks the SDK, following `tests/test_catalog.py`'s existing `@patch("providers.catalog.genai.Client")` + `_FakeApiError` style. The spec's section 1a spike was the one deliberate live verification this work needed; do not repeat it.
- **Secrets:** never print, log, or commit a credential value. A credential reaching a new code path here (`model_check.problems(credential=...)`) must never appear in an error message, a log line, or a test assertion. Follow `scripts/deploy.py::sync_env()`'s convention -- name and length, never value.
- **Two error codes, fixed spelling:** `model_not_callable` (the probe returned 404 -- this model is not usable here) and `model_probe_unavailable` (the probe could not reach a verdict -- 429, 5xx, timeout, transport failure). Never collapse the second into the first.
- **Before any push:** `uv run pytest -v` and `uv run ruff check .` must both be green (root `CLAUDE.md`). Before any push to `main`, invoke the `deploy-verify` skill. Before calling Task 7 done, invoke the `ui-visual-review` skill.
- **Commit trailer** -- every commit in this plan ends with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
  ```
- **Commit message style:** imperative sentence case, no conventional-commit prefix (`git log --oneline` for examples).
- Tasks 1-7 are this repository. Task 8 is `~/onboarding-wizard`, a separate repo that lands *after* this one is green -- see `CLAUDE.md`'s cross-repo ownership direction.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `providers/registry.py` | gains `MODEL_PROBE_POLICY` + `MODEL_PROBE_ERROR_CODES` -- the dependency-free source both `model_check` and `gen_contract` read | 1 |
| `providers/catalog.py` | gains `probe_vertex_model`, `probe_gemini_model`, `_classify_probe_exception`, `_status_of`, `_vertex_client` | 1 |
| `providers/model_check.py` | **new** -- the single `problems()` predicate every writer calls | 2 |
| `dashboard/environment.py` | four write/validate paths gate on `problems()`; `_apply_render_patch` stops flattening verdicts | 3, 4 |
| `scripts/set_override.py` | `--model` and activation probe; `--skip-probe` | 5 |
| `scripts/gen_contract.py`, `contracts/provisioning.json`, `CLAUDE.md` | publish the rule; `contract_version` 1 -> 2 | 6 |
| `dashboard/static/dashboard.html` | model joins the per-row validate gate; gemini rows gain one; two new error strings | 7 |
| `~/onboarding-wizard/{llm_client,router}.py`, `static/index.html` | the consumer half + conformance test | 8 |

---

### Task 1: The probe pair in `providers/catalog.py`

**Files:**
- Modify: `providers/registry.py` (append after `KEY_INDEX_COLUMNS`)
- Modify: `providers/catalog.py:58-90` (`_classify_exception`), `:160-200` (`list_vertex_models`)
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `registry.MODEL_PROBE_POLICY: dict[str, dict]` -- keys `gemini`/`groq`/`vertex`, each `{"required_before_write": bool, "mechanism": str | None, "reason": str | None}`
  - `registry.MODEL_PROBE_ERROR_CODES: tuple[str, str]` -- `("model_not_callable", "model_probe_unavailable")`
  - `catalog.probe_vertex_model(service_account_info: dict | None, model: str, project_override: str | None = None, location_override: str | None = None) -> CatalogResult`
  - `catalog.probe_gemini_model(api_key: str, model: str) -> CatalogResult`
  - Both return `CatalogResult(ok=True, models=None, error=None)` on success; `models` is always `None` for a probe.

- [ ] **Step 1: Add the policy table to `providers/registry.py`**

Append after `KEY_INDEX_COLUMNS`. It lives here, not in `model_check`, for a mechanical reason: `scripts/gen_contract.py` must read it, and `gen_contract` may never import the module-level `settings` instance (its own docstring, and `tests/test_provisioning_contract.py` parses its imports). `model_check` imports `providers/credentials.py`, which *does* import `settings`; `registry.py` imports nothing at all.

```python
# Whether a model must be proven callable -- not merely listed -- before it
# is written into slot_config, per provider. Read by providers/model_check.py
# (the predicate every writer calls) and published verbatim by
# scripts/gen_contract.py so the provisioner implements the same rule rather
# than a copy of its reasoning.
#
# Vertex is the reason this exists: client.models.list() returns the global
# Model Garden, not a per-project entitlement list, so "in the catalog" never
# meant "this project can call it" -- see
# docs/superpowers/specs/2026-09-11-vertex-model-entitlement-validation-design.md
# section 1. Gemini's AI-Studio listing IS key-scoped, but it exposes the same
# free countTokens call, so it is probed too rather than trusted.
#
# groq carries an explicit False with a reason rather than being omitted: an
# absent entry reads as an oversight, an explicit one reads as a decision.
MODEL_PROBE_POLICY = {
    "gemini": {
        "required_before_write": True,
        "mechanism": "count_tokens",
        "reason": None,
    },
    "groq": {
        "required_before_write": False,
        "mechanism": None,
        "reason": (
            "no free token-counting endpoint -- probing would mean a paid "
            "chat completion on every model save, and groq's own model "
            "listing is key-scoped, unlike Vertex's"
        ),
    },
    "vertex": {
        "required_before_write": True,
        "mechanism": "count_tokens",
        "reason": None,
    },
}

# The only two verdicts a probe may report. Kept apart on purpose:
# model_not_callable means the model answered 404 and is unusable here;
# model_probe_unavailable means no verdict was reached (429, 5xx, timeout).
# Collapsing the second into the first would condemn a working model because
# a provider hiccuped -- the same class of wrong answer this whole design
# exists to stop giving.
MODEL_PROBE_ERROR_CODES = ("model_not_callable", "model_probe_unavailable")
```

- [ ] **Step 2: Write the failing probe tests**

Append to `tests/test_catalog.py`. Note `_FakeApiError` and `_model` already exist at the top of that file -- reuse them, do not redefine.

```python
class TestProbeVertexModel:
    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_callable_model_returns_ok(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model(
            {"project_id": "proj-a", "token_uri": "x"}, "gemini-2.5-flash"
        )

        assert result.ok is True
        assert result.error is None
        assert result.models is None
        assert client.models.count_tokens.call_args.kwargs["model"] == "gemini-2.5-flash"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_404_is_model_not_callable(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(404)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model(
            {"project_id": "proj-a"}, "gemini-3.1-flash-lite"
        )

        assert result.ok is False
        assert result.error == "model_not_callable"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_rate_limited_is_probe_unavailable_not_model_not_callable(
        self, mock_client_cls, mock_creds
    ):
        """A provider hiccup must never be reported as an unusable model --
        that would send an operator hunting for a replacement model that was
        never the problem."""
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(429)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "model_probe_unavailable"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_server_error_is_probe_unavailable(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(503)
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "model_probe_unavailable"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_credential_refresh_failure_is_unauthorized(self, mock_client_cls, mock_creds):
        """A dead credential is a credential problem, not a model problem."""
        client = MagicMock()
        client.models.count_tokens.side_effect = google_auth_exceptions.RefreshError("nope")
        mock_client_cls.return_value = client

        result = catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "unauthorized"

    def test_no_project_anywhere_is_invalid_json(self):
        result = catalog.probe_vertex_model({}, "gemini-2.5-flash")

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_project_and_location_overrides_reach_the_client(
        self, mock_client_cls, mock_creds
    ):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        catalog.probe_vertex_model(
            {"project_id": "key-project"},
            "gemini-2.5-flash",
            project_override="other-project",
            location_override="europe-west4",
        )

        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs["project"] == "other-project"
        assert kwargs["location"] == "europe-west4"

    @patch("providers.catalog.service_account.Credentials.from_service_account_info")
    @patch("providers.catalog.genai.Client")
    def test_location_defaults_to_the_module_literal(self, mock_client_cls, mock_creds):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        catalog.probe_vertex_model({"project_id": "proj-a"}, "gemini-2.5-flash")

        assert mock_client_cls.call_args.kwargs["location"] == catalog.DEFAULT_VERTEX_LOCATION


class TestProbeGeminiModel:
    @patch("providers.catalog.genai.Client")
    def test_callable_model_returns_ok(self, mock_client_cls):
        client = MagicMock()
        client.models.count_tokens.return_value = MagicMock(total_tokens=1)
        mock_client_cls.return_value = client

        result = catalog.probe_gemini_model("fake-key", "gemini-flash-latest")

        assert result.ok is True
        assert result.error is None

    @patch("providers.catalog.genai.Client")
    def test_404_is_model_not_callable(self, mock_client_cls):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(404)
        mock_client_cls.return_value = client

        result = catalog.probe_gemini_model("fake-key", "no-such-model")

        assert result.ok is False
        assert result.error == "model_not_callable"

    @patch("providers.catalog.genai.Client")
    def test_unauthorized_key_is_not_a_model_verdict(self, mock_client_cls):
        client = MagicMock()
        client.models.count_tokens.side_effect = _FakeApiError(401)
        mock_client_cls.return_value = client

        result = catalog.probe_gemini_model("bad-key", "gemini-flash-latest")

        assert result.ok is False
        assert result.error == "unauthorized"
```

Add this import at the top of `tests/test_catalog.py` (next to the existing imports):

```python
from google.auth import exceptions as google_auth_exceptions
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_catalog.py -k "Probe" -v`
Expected: FAIL with `AttributeError: module 'providers.catalog' has no attribute 'probe_vertex_model'`.

- [ ] **Step 4: Extract the status reader and the Vertex client builder**

In `providers/catalog.py`, replace the body of `_classify_exception` with a call to a new `_status_of` helper, keeping its existing docstring on `_status_of` (the docstring explains the attribute-order reasoning, which belongs with the code that does the reading):

```python
def _status_of(exc: Exception) -> int | None:
    # Duck-typed on purpose: rather than depend on each SDK's own exception
    # class hierarchy (google-genai's and groq's differ, and either could
    # change shape across versions), read whichever HTTP-status-shaped
    # attribute is present. .status_code checked first: some SDK generations
    # (Stainless-based ones, e.g. openai-python's APIError) set a STRING
    # error code on `.code` (e.g. "invalid_api_key") that would otherwise
    # short-circuit an `or` chain and silently misclassify a real 401/429 as
    # provider_unreachable. Falls back to exc.response.status_code last --
    # requests.exceptions.HTTPError (raised by list_accessible_projects'
    # raise_for_status()) carries its status there instead of on the
    # exception itself.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
    return status if isinstance(status, int) else None


def _classify_exception(exc: Exception) -> str:
    return _classify_status(_status_of(exc))
```

Then extract the client builder both `list_vertex_models` and `probe_vertex_model` need. Add it directly above `list_vertex_models`:

```python
def _vertex_client(
    service_account_info: dict | None,
    project_override: str | None,
    location_override: str | None,
) -> tuple[genai.Client | None, str | None]:
    """The Vertex client for this (credential, project, location), or a
    structural error code. Shared by list_vertex_models and
    probe_vertex_model so the two can never disagree about which project,
    location or credential a verdict was obtained against -- the listing
    saying one thing and the probe another would be worse than either alone.

    `location_override` unset falls back to DEFAULT_VERTEX_LOCATION -- a
    LITERAL, not a Settings read. settings.vertex_gcp_location is no longer
    authoritative (it's only a seed value for slot_config -- see
    docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-
    design.md section 10.4) and doesn't exist as an env var on the deployed
    service at all, so reading it here silently used whatever the operator's
    local .env.config happened to say. This fallback exists only for these
    read-only catalog/probe calls; the review path (providers/factory.py)
    has no location fallback at all, by design.
    """
    project = project_override or (service_account_info or {}).get("project_id", "")
    if not project:
        return None, "invalid_service_account_json"
    location = location_override or DEFAULT_VERTEX_LOCATION

    creds = None
    if service_account_info is not None:
        try:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=_VERTEX_SCOPES
            )
        except Exception:  # noqa: BLE001 -- malformed key content, not an HTTP failure
            return None, "invalid_service_account_json"

    return (
        genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS),
        ),
        None,
    )
```

Rewrite `list_vertex_models`'s body to use it, keeping its own docstring's first paragraph and deleting the `location_override` paragraph now living on `_vertex_client`:

```python
def list_vertex_models(
    service_account_info: dict | None,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    """Every model Vertex lists for this credential -- which is NOT the same
    as every model this project may call: see _list_generative_models.
    Client construction, including the location fallback, is _vertex_client's."""
    try:
        client, error = _vertex_client(
            service_account_info, project_override, location_override
        )
        if error:
            return CatalogResult(ok=False, models=None, error=error)
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001
        error = _classify_vertex_auth_exception(exc) or _classify_exception(exc)
        return CatalogResult(ok=False, models=None, error=error)
    return CatalogResult(ok=True, models=models, error=None)
```

- [ ] **Step 5: Run the existing catalog tests to confirm the refactor broke nothing**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: every pre-existing test PASSES; only the new `Probe` tests still fail.

- [ ] **Step 6: Write the probes**

Add to `providers/catalog.py`, after `list_vertex_models`:

```python
# What a probe sends. One token of throwaway text: countTokens neither
# generates nor bills, so the content is irrelevant -- only whether the
# publisher model resolves for this project.
_PROBE_CONTENTS = "ping"


def _classify_probe_exception(exc: Exception) -> str:
    """A probe failure's structural code.

    The 404 branch is the whole point: Vertex answers a model this project
    may not call with 404 NOT_FOUND, byte-for-byte the error a real
    generateContent gets (verified live -- see the design spec's section
    1a). Every other failure means no verdict was reached, which is NOT the
    same answer and must never be reported as one. 401/403 keep their
    credential-shaped classification so an operator is not told to change
    the model when the key is the problem.
    """
    auth = _classify_vertex_auth_exception(exc)
    if auth is not None:
        return auth
    status = _status_of(exc)
    if status == 404:
        return "model_not_callable"
    if status in (401, 403):
        return _classify_status(status)
    return "model_probe_unavailable"


def probe_vertex_model(
    service_account_info: dict | None,
    model: str,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    """Whether `model` is actually callable for this project/location.

    THE question list_vertex_models cannot answer. Vertex's own listing is
    the global Model Garden -- entitlement is enforced only when a request
    names the publisher model -- so membership in it proves the credential
    authenticates and nothing more. countTokens resolves the publisher model
    exactly as generateContent does, for free and without generating
    anything, which makes it the cheapest honest answer available.

    `models` is always None: this reports a verdict, not a catalog.
    """
    try:
        client, error = _vertex_client(
            service_account_info, project_override, location_override
        )
        if error:
            return CatalogResult(ok=False, models=None, error=error)
        client.models.count_tokens(model=model, contents=_PROBE_CONTENTS)
    except Exception as exc:  # noqa: BLE001 -- classified structurally above
        return CatalogResult(ok=False, models=None, error=_classify_probe_exception(exc))
    return CatalogResult(ok=True, models=None, error=None)


def probe_gemini_model(api_key: str, model: str) -> CatalogResult:
    """Whether `model` is callable with this AI-Studio key.

    Gemini's listing IS key-scoped, unlike Vertex's, so this is a narrower
    guarantee than probe_vertex_model's -- but countTokens is free here too,
    and a verdict obtained the same way the review path obtains its own beats
    one inferred from a listing.
    """
    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS)
        )
        client.models.count_tokens(model=model, contents=_PROBE_CONTENTS)
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_probe_exception(exc))
    return CatalogResult(ok=True, models=None, error=None)
```

- [ ] **Step 7: Correct `_list_generative_models`'s docstring**

It currently has none in this repo; the reasoning it inherited from the wizard's copy is what made this bug possible. Add:

```python
def _list_generative_models(client: genai.Client) -> list[str]:
    """Every model the client lists, filtered to generateContent-capable ones
    where that capability is known.

    Vertex responses never populate Model.supported_actions
    (_Model_from_vertex has no mapping for it), so a Vertex model is always
    let through rather than dropped -- dropping it would silently empty the
    entire Vertex catalog regardless of credential.

    WHAT THIS LIST IS NOT: for Vertex it is essentially the global Model
    Garden, not a per-project entitlement list -- a live listing returned
    veo-*, lyria-* and medgemma entries this project plainly cannot call.
    Membership here means the credential authenticates. Whether this project
    may actually generate with a given model is probe_vertex_model's
    question, and only a real call can answer it. Treating the two as the
    same fact put a 404-ing model into production -- see the design spec's
    section 1.
    """
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: PASS, all tests.

- [ ] **Step 9: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`
Expected: both green.

- [ ] **Step 10: Commit**

```bash
git add providers/registry.py providers/catalog.py tests/test_catalog.py
git commit -m "$(cat <<'EOF'
Add countTokens entitlement probes for Vertex and Gemini models

Vertex's models.list() returns the global Model Garden, not a per-project
entitlement list, so "in the catalog" never meant "this project can call
it". countTokens resolves the publisher model exactly as generateContent
does -- free, generating nothing -- and returns the identical 404 for a
model the project may not use.

model_not_callable and model_probe_unavailable stay distinct: a 429 or a
timeout reached no verdict, and reporting that as an unusable model would
send an operator hunting for a replacement that was never the problem.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 2: `providers/model_check.py` -- the one predicate

**Files:**
- Create: `providers/model_check.py`
- Test: `tests/test_model_check.py` (new)

**Interfaces:**
- Consumes: `catalog.probe_vertex_model`, `catalog.probe_gemini_model`, `registry.MODEL_PROBE_POLICY` (Task 1); existing `credentials.resolve(provider, index) -> tuple[str, str]`, `vertex_credentials.resolve_service_account_info(index) -> dict | None`.
- Produces: `model_check.problems(provider: str, slot: int, model: str, vertex_gcp_project: str | None = None, vertex_gcp_location: str | None = None, credential: str | dict | None = None) -> list[str]` -- empty list means usable.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_check.py`:

```python
"""Mocked tests for providers/model_check.py -- the single predicate every
slot_config writer gates on. No live calls (root CLAUDE.md)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from providers import catalog, model_check


def _ok():
    return catalog.CatalogResult(ok=True, models=None, error=None)


def _fail(error: str):
    return catalog.CatalogResult(ok=False, models=None, error=error)


class TestGroqIsNeverProbed:
    def test_groq_returns_no_problems_without_calling_any_probe(self):
        with patch.object(catalog, "probe_gemini_model") as probe:
            assert model_check.problems("groq", 0, "llama-3.3-70b-versatile") == []
            probe.assert_not_called()


class TestVertex:
    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_callable_model_has_no_problems(self, resolve, probe):
        resolve.return_value = {"project_id": "proj-a"}
        probe.return_value = _ok()

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == []

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_uncallable_model_reports_the_probe_code(self, resolve, probe):
        resolve.return_value = {"project_id": "proj-a"}
        probe.return_value = _fail("model_not_callable")

        assert model_check.problems("vertex", 0, "gemini-3.1-flash-lite") == [
            "model_not_callable"
        ]

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_probe_unavailable_is_a_problem_too(self, resolve, probe):
        """Fail closed: an unreachable provider must not let an unverified
        model through, or 'saved unverified' silently becomes 'saved broken'
        -- nothing re-checks it later."""
        resolve.return_value = {"project_id": "proj-a"}
        probe.return_value = _fail("model_probe_unavailable")

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == [
            "model_probe_unavailable"
        ]

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_slot_project_and_location_reach_the_probe(self, resolve, probe):
        resolve.return_value = {"project_id": "key-project"}
        probe.return_value = _ok()

        model_check.problems(
            "vertex",
            2,
            "gemini-2.5-flash",
            vertex_gcp_project="other-project",
            vertex_gcp_location="europe-west4",
        )

        assert probe.call_args.kwargs["project_override"] == "other-project"
        assert probe.call_args.kwargs["location_override"] == "europe-west4"
        assert resolve.call_args.args[0] == 2

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_malformed_stored_credential_is_structural_not_a_crash(self, resolve, probe):
        resolve.side_effect = ValueError("not base64")

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == [
            "invalid_service_account_json"
        ]
        probe.assert_not_called()

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_no_key_and_no_project_is_no_credential_configured(self, resolve, probe):
        """Mirrors dashboard _safe_resolve_vertex_info: a missing key is only
        a problem when the slot has no project either, since without either
        there is nothing for implicit ADC to resolve against."""
        resolve.return_value = None

        assert model_check.problems("vertex", 0, "gemini-2.5-flash") == [
            "no_credential_configured"
        ]
        probe.assert_not_called()

    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_no_key_but_a_project_falls_through_to_implicit_adc(self, resolve, probe):
        resolve.return_value = None
        probe.return_value = _ok()

        assert model_check.problems(
            "vertex", 0, "gemini-2.5-flash", vertex_gcp_project="proj-a"
        ) == []
        assert probe.call_args.args[0] is None


class TestExplicitCredential:
    @patch.object(catalog, "probe_vertex_model")
    @patch("providers.model_check.vertex_credentials.resolve_service_account_info")
    def test_passed_credential_is_probed_and_the_slot_is_never_resolved(
        self, resolve, probe
    ):
        """Guided setup probes a credential that exists nowhere the slot
        could resolve it from yet. Resolving the slot here would probe the
        PREVIOUS credential -- or none at all on a first-time setup -- and
        answer a question nobody asked. resolve raising is the assertion."""
        resolve.side_effect = AssertionError("must not resolve the slot")
        probe.return_value = _ok()

        assert model_check.problems(
            "vertex", 0, "gemini-2.5-flash", credential={"project_id": "fresh"}
        ) == []
        assert probe.call_args.args[0] == {"project_id": "fresh"}

    @patch.object(catalog, "probe_gemini_model")
    @patch("providers.model_check.credentials.resolve")
    def test_passed_api_key_is_probed_for_gemini(self, resolve, probe):
        resolve.side_effect = AssertionError("must not resolve the slot")
        probe.return_value = _ok()

        assert model_check.problems(
            "gemini", 0, "gemini-flash-latest", credential="fresh-key"
        ) == []
        assert probe.call_args.args[0] == "fresh-key"


class TestGemini:
    @patch.object(catalog, "probe_gemini_model")
    @patch("providers.model_check.credentials.resolve")
    def test_missing_key_is_no_credential_configured(self, resolve, probe):
        resolve.return_value = ("GEMINI_API_KEY", "")

        assert model_check.problems("gemini", 0, "gemini-flash-latest") == [
            "no_credential_configured"
        ]
        probe.assert_not_called()


class TestStructuralGuards:
    def test_unknown_provider(self):
        assert model_check.problems("openai", 0, "gpt-5") == ["unknown_provider"]

    @pytest.mark.parametrize("model", ["", None])
    def test_empty_model_is_a_problem_before_any_probe(self, model):
        with patch.object(catalog, "probe_vertex_model") as probe:
            assert model_check.problems("vertex", 0, model) == ["model_required"]
            probe.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_model_check.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'providers.model_check'`.

- [ ] **Step 3: Write the module**

Create `providers/model_check.py`:

```python
"""Is this model actually usable for this slot? -- the one predicate every
slot_config writer calls.

One predicate rather than a check per caller, for the reason root CLAUDE.md
already records against store.py's cooldown/usage-cap writers: a rule that
lives with one caller is a validation gap waiting for its second caller.
dashboard/environment.py had two slot_config writers and neither checked the
model at all; the interactive validate endpoint that did check was a separate
call the frontend chose to make first. Every writer now gates here --
the guided-setup Apply, the per-slot config PATCH, the arming paths, and
scripts/set_override.py -- so none of them can disagree about what a usable
model is.

Lives in providers/ rather than dashboard/ because scripts/set_override.py is
one of its callers, and the CLI must not depend on the dashboard package.

See docs/superpowers/specs/2026-09-11-vertex-model-entitlement-validation-
design.md.
"""

from __future__ import annotations

from providers import catalog, credentials, registry, vertex_credentials


def problems(
    provider: str,
    slot: int,
    model: str,
    vertex_gcp_project: str | None = None,
    vertex_gcp_location: str | None = None,
    credential: str | dict | None = None,
) -> list[str]:
    """Every reason `model` is unusable for this slot, as structural error
    codes. An empty list means usable.

    `credential` left None resolves the slot's own stored credential, which
    is what the config panel, the CLI, and any re-validate of an
    already-configured slot want. Passed explicitly, it is probed instead --
    guided setup REQUIRES this: it holds a freshly-uploaded credential in its
    own request body and must probe before pushing it to Render, so at probe
    time that credential exists nowhere the slot could resolve it from.
    Resolving the slot there would silently probe the previous credential, or
    none at all on a first-time setup. Shape is whatever the family already
    uses: a raw API-key string for gemini, the decoded service-account dict
    for vertex.

    Fails closed. A probe that could not run (model_probe_unavailable) is a
    problem, not a pass: an operator save is a retryable foreground action,
    and letting an unverified model through on a transient 429 would leave a
    "saved unverified" state that nothing ever re-checks.
    """
    if provider not in registry.PROVIDERS:
        return ["unknown_provider"]
    if not model:
        return ["model_required"]
    if not registry.MODEL_PROBE_POLICY[provider]["required_before_write"]:
        # groq: no free token-counting endpoint, and its own listing is
        # key-scoped -- see registry.MODEL_PROBE_POLICY's reason field.
        return []

    if provider == "vertex":
        info = credential
        if info is None:
            try:
                info = vertex_credentials.resolve_service_account_info(slot)
            except ValueError:
                # Covers json.JSONDecodeError, binascii.Error and
                # UnicodeDecodeError too -- all ValueError subclasses.
                return ["invalid_service_account_json"]
            if info is None and not vertex_gcp_project:
                # No explicit key AND no project: nothing for implicit ADC to
                # resolve against. Mirrors dashboard's _safe_resolve_vertex_info
                # and providers/factory.py's own definition of "configured".
                return ["no_credential_configured"]
        result = catalog.probe_vertex_model(
            info,
            model,
            project_override=vertex_gcp_project or None,
            location_override=vertex_gcp_location or None,
        )
    else:
        api_key = credential
        if api_key is None:
            _, api_key = credentials.resolve(provider, slot)
        if not api_key:
            return ["no_credential_configured"]
        result = catalog.probe_gemini_model(api_key, model)

    if result.ok:
        return []
    return [result.error or "model_probe_unavailable"]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_model_check.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add providers/model_check.py tests/test_model_check.py
git commit -m "$(cat <<'EOF'
Add model_check.problems, the predicate every slot_config writer will gate on

One predicate rather than a check per caller, for the reason CLAUDE.md
already records against store.py's cooldown writers: a rule living with one
caller is a validation gap waiting for its second caller. Fails closed on an
unreachable probe -- a "saved unverified" state is one nothing re-checks.

The credential override exists for guided setup specifically, which probes a
credential that exists nowhere the slot could resolve it from yet.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 3: Gate the dashboard's model write and validate paths

**Files:**
- Modify: `dashboard/environment.py:107-133` (`_validate_model_var`), `:355-430` (`_apply_llm_credential`), `:533-546` (`_apply_render_patch`'s sets loop), `:904-940` (`_apply_slot_config_patch`)
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `model_check.problems(...)` (Task 2).
- Produces: the three write paths return `{"applied": [...], "failed": [{"key": ..., "error": "<probe code>"}]}`; `_validate_model_var` returns `{"ok": False, "error": "<probe code>", "models": [...]}` with the listing preserved.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/tests/test_environment.py`. Follow the file's existing monkeypatch style (`monkeypatch.setattr(catalog, "list_vertex_models", ...)`); add `model_check` to its imports from `providers`.

```python
class TestModelProbeGatesEveryWritePath:
    @pytest.mark.asyncio
    async def test_validate_model_var_rejects_a_listed_but_uncallable_model(
        self, monkeypatch
    ):
        """The exact production shape: the model IS in the catalog and still
        404s. The listing must still come back so the dropdown survives."""
        monkeypatch.setattr(
            catalog,
            "list_vertex_models",
            lambda *a, **k: catalog.CatalogResult(
                ok=True, models=["gemini-2.5-flash", "gemini-3.1-flash-lite"], error=None
            ),
        )
        monkeypatch.setattr(
            environment.model_check, "problems", lambda *a, **k: ["model_not_callable"]
        )
        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 0})
        monkeypatch.setattr(store, "get_slot_config", lambda *a: {"model": "x"})
        monkeypatch.setattr(
            environment, "_safe_resolve_vertex_info", lambda slot: ({"project_id": "p"}, None)
        )

        result = await asyncio.to_thread(
            environment._validate_model_var, "vertex", "gemini-3.1-flash-lite"
        )

        assert result["ok"] is False
        assert result["error"] == "model_not_callable"
        assert result["models"] == ["gemini-2.5-flash", "gemini-3.1-flash-lite"]

    @pytest.mark.asyncio
    async def test_apply_llm_credential_refuses_before_pushing_the_credential(
        self, monkeypatch
    ):
        """Ordering matters: a refused model must never leave a pushed
        credential with no matching slot_config row."""
        pushed = []
        monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
        monkeypatch.setattr(
            render_client, "push_env_var", lambda *a: pushed.append(a)
        )

        def _never(*a, **k):
            raise AssertionError("slot_config must not be written")

        monkeypatch.setattr(store, "set_slot_config", _never)
        monkeypatch.setattr(
            environment.model_check, "problems", lambda *a, **k: ["model_not_callable"]
        )

        payload = environment.ApplyLlmCredentialRequest(
            slot=0,
            credential={"api_key": "k"},
            model="no-such-model",
            vertex_gcp_project=None,
            vertex_gcp_location=None,
        )
        result = await asyncio.to_thread(
            environment._apply_llm_credential, "gemini", payload
        )

        assert pushed == []
        assert result["failed"] == [
            {"key": "slot_config.gemini.0", "error": "model_not_callable"}
        ]

    @pytest.mark.asyncio
    async def test_apply_llm_credential_probes_the_request_body_credential(
        self, monkeypatch
    ):
        seen = {}

        def _problems(provider, slot, model, **kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(environment.model_check, "problems", _problems)
        monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
        monkeypatch.setattr(render_client, "push_env_var", lambda *a: None)
        monkeypatch.setattr(store, "set_slot_config", lambda *a, **k: None)

        payload = environment.ApplyLlmCredentialRequest(
            slot=0,
            credential={"api_key": "fresh-key"},
            model="gemini-flash-latest",
            vertex_gcp_project=None,
            vertex_gcp_location=None,
        )
        await asyncio.to_thread(environment._apply_llm_credential, "gemini", payload)

        assert seen["credential"] == "fresh-key"

    @pytest.mark.asyncio
    async def test_apply_slot_config_patch_refuses_an_uncallable_model(
        self, monkeypatch
    ):
        def _never(*a, **k):
            raise AssertionError("slot_config must not be written")

        monkeypatch.setattr(store, "set_slot_config", _never)
        monkeypatch.setattr(store, "get_slot_config", lambda *a: {
            "model": "old", "vertex_gcp_project": "p", "vertex_gcp_location": "us-central1"
        })
        monkeypatch.setattr(
            environment.model_check, "problems", lambda *a, **k: ["model_probe_unavailable"]
        )

        patch_payload = environment.SlotConfigPatch(
            provider="vertex", slot=0, model="gemini-3.1-flash-lite"
        )
        result = await asyncio.to_thread(
            environment._apply_slot_config_patch, patch_payload
        )

        assert result["applied"] == []
        assert result["failed"] == [
            {"key": "slot_config.vertex.0", "error": "model_probe_unavailable"}
        ]

    @pytest.mark.asyncio
    async def test_render_patch_propagates_the_probe_code_not_failed_validation(
        self, monkeypatch
    ):
        """Flattening every verdict to failed_validation would make the two
        new UI strings dead code on this path."""
        monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
        monkeypatch.setattr(
            environment,
            "_validate_var",
            lambda var, value: {"ok": False, "error": "model_not_callable", "models": None},
        )

        result = await asyncio.to_thread(
            environment._apply_render_patch,
            environment.EnvironmentRenderPatch(sets={"VERTEX_MODEL": "gemini-3.1-flash-lite"}),
        )

        assert result["failed"] == [
            {"key": "VERTEX_MODEL", "error": "model_not_callable"}
        ]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest dashboard/tests/test_environment.py -k ModelProbeGates -v`
Expected: FAIL -- `AttributeError: module 'dashboard.environment' has no attribute 'model_check'`.

- [ ] **Step 3: Import the predicate**

In `dashboard/environment.py`, extend the existing providers import:

```python
from providers import catalog, credentials, model_check, registry, vertex_credentials
```

- [ ] **Step 4: Gate `_validate_model_var`**

Replace its final three lines (the `not_in_catalog` check and the success return):

```python
    if not result.ok:
        return {"ok": False, "error": result.error, "models": None}
    if candidate not in (result.models or []):
        return {"ok": False, "error": "not_in_catalog", "models": result.models}
    # Being in the catalog is necessary and NOT sufficient -- for vertex the
    # listing is the global Model Garden, so this is where a listed model is
    # actually proven callable. The listing is returned either way: the
    # dropdown it populates should survive a rejected candidate.
    issues = model_check.problems(
        provider,
        slot,
        candidate,
        vertex_gcp_project=row.get("vertex_gcp_project") if provider == "vertex" else None,
        vertex_gcp_location=row.get("vertex_gcp_location") if provider == "vertex" else None,
    )
    if issues:
        return {"ok": False, "error": issues[0], "models": result.models}
    return {"ok": True, "error": None, "models": result.models}
```

Note: `row` is only assigned inside the `provider == "vertex"` branch today. Hoist `row = {}` to just above the `if provider == "vertex":` line so the call above is valid for every provider.

- [ ] **Step 5: Gate `_apply_llm_credential`**

Move the `credential_value` assignment above the Render lookup, then insert the probe immediately after the existing `vertex_gcp_location_required` guard and **before** `render_client.find_service_id()`:

```python
    credential_value = (
        payload.credential.get("service_account_b64", "")
        if family == "vertex"
        else payload.credential.get("api_key", "")
    )

    # Probe the credential in THIS request body, not the slot's stored one:
    # the uploaded credential has not been pushed to Render yet, so the slot
    # cannot resolve it. Runs before the push for the same reason the
    # location guard above does -- a refused model must never leave a pushed
    # credential with no matching slot_config row.
    probe_credential: str | dict | None = credential_value or None
    if family == "vertex" and credential_value:
        try:
            probe_credential = json.loads(
                base64.b64decode(credential_value, validate=True).decode()
            )
        except (ValueError, UnicodeDecodeError):
            # ValueError covers binascii.Error and json.JSONDecodeError.
            return {
                "applied": [],
                "failed": [
                    {"key": slot_config_key, "error": "invalid_service_account_json"}
                ],
            }
    issues = model_check.problems(
        family,
        payload.slot,
        payload.model,
        vertex_gcp_project=payload.vertex_gcp_project,
        vertex_gcp_location=payload.vertex_gcp_location,
        credential=probe_credential,
    )
    if issues:
        return {"applied": [], "failed": [{"key": slot_config_key, "error": issues[0]}]}
```

Delete the now-duplicated `credential_value = (...)` assignment further down.

- [ ] **Step 6: Gate `_apply_slot_config_patch`**

Insert after its existing `vertex_gcp_location_required` guard, before `store.set_slot_config`:

```python
    issues = model_check.problems(
        payload.provider,
        payload.slot,
        model,
        vertex_gcp_project=project,
        vertex_gcp_location=location,
    )
    if issues:
        return {"applied": [], "failed": [{"key": key, "error": issues[0]}]}
```

- [ ] **Step 7: Stop `_apply_render_patch` flattening the verdict**

In its `payload.sets` loop, replace the two `failed.append({"key": key, "error": "failed_validation"})` lines' second occurrence (the `if not check["ok"]` branch) with:

```python
            if not check["ok"]:
                # Propagate the real code (model_not_callable /
                # model_probe_unavailable / not_in_catalog) rather than
                # flattening it: the dashboard maps each to its own message,
                # and "failed_validation" tells an operator nothing about
                # whether to pick a different model or simply retry.
                failed.append({"key": key, "error": check["error"] or "failed_validation"})
                continue
```

Leave the `except` branch's `failed_validation` alone -- an unexpected raise genuinely has no structural code.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS. If a pre-existing test now fails because it saves a model without stubbing `model_check.problems`, add the stub to that test rather than weakening the gate.

- [ ] **Step 9: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`

- [ ] **Step 10: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "$(cat <<'EOF'
Gate the dashboard's model write paths on a real entitlement probe

Neither _apply_llm_credential nor _apply_slot_config_patch checked the model
at all -- the validate endpoint was a separate call the frontend chose to
make first, so the gate was a frontend courtesy any non-UI request bypassed.

_apply_llm_credential probes the credential in its own request body, since
the uploaded credential is not in the slot yet, and probes before the Render
push so a refusal cannot strand a pushed credential without its row.

_apply_render_patch stops flattening every verdict to failed_validation,
which would have left the two new error messages unreachable on that path.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 4: Gate arming -- `provider` and `key_index`

**Files:**
- Modify: `dashboard/environment.py:753-770` (the `provider` branch of `_apply_config_patch`), `:839-850` (its `key_index` loop)
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `model_check.problems(...)` (Task 2).
- Produces: `environment._arming_probe_targets(fields: dict) -> dict[tuple[str, int], list[str]]` -- changed armings mapped to the response field keys that requested them.

- [ ] **Step 1: Write the failing tests**

```python
class TestArmingIsGatedToo:
    @pytest.mark.asyncio
    async def test_unchanged_key_index_triggers_no_probe(self, monkeypatch):
        """saveConfig() re-sends every provider's key_index on every save, so
        an unscoped rule would fire three live calls when someone edits a
        cooldown value."""
        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 1})
        monkeypatch.setattr(store, "get_provider_override", lambda: "vertex")
        monkeypatch.setattr(store, "set_key_index_override", lambda *a: None)

        def _never(*a, **k):
            raise AssertionError("an unchanged arming must not be probed")

        monkeypatch.setattr(environment.model_check, "problems", _never)

        result = await asyncio.to_thread(
            environment._apply_config_patch,
            environment.EnvironmentConfigPatch(key_index={"vertex": 1}),
        )

        assert result["failed"] == []

    @pytest.mark.asyncio
    async def test_changed_key_index_probes_the_target_slots_stored_model(
        self, monkeypatch
    ):
        seen = {}

        def _problems(provider, slot, model, **kwargs):
            seen.update({"provider": provider, "slot": slot, "model": model, **kwargs})
            return []

        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 0})
        monkeypatch.setattr(store, "get_provider_override", lambda: "vertex")
        monkeypatch.setattr(store, "set_key_index_override", lambda *a: None)
        monkeypatch.setattr(store, "get_slot_config", lambda *a: {
            "model": "gemini-2.5-flash",
            "vertex_gcp_project": "proj-b",
            "vertex_gcp_location": "europe-west4",
        })
        monkeypatch.setattr(environment.model_check, "problems", _problems)

        await asyncio.to_thread(
            environment._apply_config_patch,
            environment.EnvironmentConfigPatch(key_index={"vertex": 1}),
        )

        assert seen["slot"] == 1
        assert seen["model"] == "gemini-2.5-flash"
        assert seen["vertex_gcp_project"] == "proj-b"
        assert seen["vertex_gcp_location"] == "europe-west4"

    @pytest.mark.asyncio
    async def test_failed_arming_probe_fails_only_that_field(self, monkeypatch):
        """The cooldown values riding in the same PATCH must still apply --
        _apply_config_patch has always applied each group independently."""
        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 0})
        monkeypatch.setattr(store, "get_provider_override", lambda: "vertex")
        monkeypatch.setattr(store, "get_slot_config", lambda *a: {"model": "bad-model"})
        monkeypatch.setattr(store, "get_cooldown_overrides", lambda: (300.0, 3600.0, 2.0))
        applied_cooldown = []
        monkeypatch.setattr(
            store, "set_cooldown_override", lambda *a: applied_cooldown.append(a)
        )

        def _never_set(*a):
            raise AssertionError("a refused arming must not be written")

        monkeypatch.setattr(store, "set_key_index_override", _never_set)
        monkeypatch.setattr(
            environment.model_check, "problems", lambda *a, **k: ["model_not_callable"]
        )

        result = await asyncio.to_thread(
            environment._apply_config_patch,
            environment.EnvironmentConfigPatch(
                key_index={"vertex": 1}, cooldown_base_seconds=120.0
            ),
        )

        assert {"key": "key_index.vertex", "error": "model_not_callable"} in result["failed"]
        assert "cooldown_base_seconds" in result["applied"]
        assert applied_cooldown

    @pytest.mark.asyncio
    async def test_changed_provider_probes_its_effective_slot(self, monkeypatch):
        seen = {}

        def _problems(provider, slot, model, **kwargs):
            seen.update({"provider": provider, "slot": slot})
            return []

        monkeypatch.setattr(store, "get_provider_override", lambda: "gemini")
        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 2})
        monkeypatch.setattr(store, "get_slot_config", lambda *a: {"model": "gemini-2.5-flash"})
        monkeypatch.setattr(store, "set_provider_override", lambda *a: None)
        monkeypatch.setattr(environment.model_check, "problems", _problems)

        await asyncio.to_thread(
            environment._apply_config_patch,
            environment.EnvironmentConfigPatch(provider="vertex"),
        )

        assert seen == {"provider": "vertex", "slot": 2}

    @pytest.mark.asyncio
    async def test_provider_and_key_index_for_one_provider_probe_once(self, monkeypatch):
        calls = []

        monkeypatch.setattr(store, "get_provider_override", lambda: "gemini")
        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 0})
        monkeypatch.setattr(store, "get_slot_config", lambda *a: {"model": "m"})
        monkeypatch.setattr(store, "set_provider_override", lambda *a: None)
        monkeypatch.setattr(store, "set_key_index_override", lambda *a: None)
        monkeypatch.setattr(
            environment.model_check,
            "problems",
            lambda provider, slot, model, **k: calls.append((provider, slot)) or [],
        )

        await asyncio.to_thread(
            environment._apply_config_patch,
            environment.EnvironmentConfigPatch(provider="vertex", key_index={"vertex": 1}),
        )

        assert calls == [("vertex", 1)]

    @pytest.mark.asyncio
    async def test_clearing_an_override_to_none_is_not_an_arming(self, monkeypatch):
        """key_index=None clears the override; there is no target slot to
        probe, and refusing a clear would block a key rotation."""
        monkeypatch.setattr(store, "get_all_key_index_overrides", lambda: {"vertex": 1})
        monkeypatch.setattr(store, "get_provider_override", lambda: "vertex")
        monkeypatch.setattr(store, "set_key_index_override", lambda *a: None)

        def _never(*a, **k):
            raise AssertionError("clearing must not be probed")

        monkeypatch.setattr(environment.model_check, "problems", _never)

        result = await asyncio.to_thread(
            environment._apply_config_patch,
            environment.EnvironmentConfigPatch(key_index={"vertex": None}),
        )

        assert result["failed"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest dashboard/tests/test_environment.py -k ArmingIsGated -v`
Expected: FAIL -- the probes are never called, or `_never` fires.

- [ ] **Step 3: Add the target resolver**

Add above `_apply_config_patch` in `dashboard/environment.py`:

```python
def _arming_probe_targets(fields: dict) -> dict[tuple[str, int], list[str]]:
    """The (provider, slot) pairs this PATCH would newly arm, mapped to the
    response field keys that asked for them.

    Neither `provider` nor `key_index` carries a model value, so neither
    looks like a model write -- but both change WHICH slot_config row is
    live, and slot N's credential, project and location all differ from slot
    0's. A model that probed clean in slot 0 is an open question in slot 1.
    Flipping either is therefore a third route to "the UI reported applied,
    every subsequent review 404s".

    Scoped to what actually CHANGED. The config form re-sends every
    provider's key_index on every save, unconditionally and by design
    (dashboard.html's saveConfig), so an unscoped rule would fire a live call
    per provider every time someone edits a cooldown value. A clear
    (index None) has no target slot and is never probed -- refusing one
    would block a key rotation, which is exactly when an operator needs it.
    """
    stored_indexes = store.get_all_key_index_overrides()
    submitted_indexes = fields.get("key_index", {}) or {}
    targets: dict[tuple[str, int], list[str]] = {}

    for provider, index in submitted_indexes.items():
        if provider not in registry.PROVIDERS or index is None:
            continue
        if index == stored_indexes.get(provider):
            continue
        targets.setdefault((provider, index), []).append(f"key_index.{provider}")

    if "provider" in fields:
        provider = fields["provider"]
        if provider in registry.PROVIDERS and provider != store.get_provider_override():
            # The slot this provider will actually run against: a key_index
            # for it in this same PATCH wins over the stored one.
            submitted = submitted_indexes.get(provider)
            slot = submitted if submitted is not None else (stored_indexes.get(provider) or 0)
            targets.setdefault((provider, slot), []).append("provider")

    return targets


def _arming_problems(fields: dict) -> dict[str, str]:
    """Field key -> error code, for every arming this PATCH cannot honour."""
    failures: dict[str, str] = {}
    for (provider, slot), field_keys in _arming_probe_targets(fields).items():
        row = store.get_slot_config(provider, slot) or {}
        issues = model_check.problems(
            provider,
            slot,
            row.get("model") or "",
            vertex_gcp_project=row.get("vertex_gcp_project"),
            vertex_gcp_location=row.get("vertex_gcp_location"),
        )
        if issues:
            for field_key in field_keys:
                failures[field_key] = issues[0]
    return failures
```

- [ ] **Step 4: Consult it in `_apply_config_patch`**

Immediately after `fields = payload.model_dump(exclude_unset=True)` and the `applied`/`failed` initialisation, add:

```python
    # One probe pass up front, so a provider change and a key_index change
    # naming the same slot cost one live call, not two.
    arming_failures = _arming_problems(fields)
```

In the `provider` branch, replace the `else:` body's first line so the write is skipped when refused:

```python
    if "provider" in fields:
        provider = fields["provider"]
        if provider is not None and provider not in registry.PROVIDERS:
            failed.append({"key": "provider", "error": "unknown_provider"})
        elif "provider" in arming_failures:
            failed.append({"key": "provider", "error": arming_failures["provider"]})
        else:
            try:
                store.set_provider_override(provider, now)
                applied.append("provider")
            except Exception as exc:  # noqa: BLE001
                failed.append({"key": "provider", "error": type(exc).__name__})
```

In the `key_index` loop, add the same check after the existing `invalid_slot` guard:

```python
        field_key = f"key_index.{provider}"
        if field_key in arming_failures:
            failed.append({"key": field_key, "error": arming_failures[field_key]})
            continue
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS.

- [ ] **Step 6: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`

- [ ] **Step 7: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "$(cat <<'EOF'
Probe the target slot when a provider or key-slot change arms it

Neither field carries a model value, so neither looked like a model write --
but both change which slot_config row is live, and slot N's credential,
project and location all differ from slot 0's. A model that probed clean in
slot 0 is an open question in slot 1.

Scoped to a changed arming: the config form re-sends every provider's
key_index on every save, so an unscoped rule would cost three live calls
every time someone edits a cooldown. A refused arming fails that field alone,
leaving the rest of the PATCH to apply.

This doubles as the retroactive catch for rows written before this gate
existed -- a stale bad row is caught the moment anyone arms it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 5: `scripts/set_override.py` -- probe `--model` and activation

**Files:**
- Modify: `scripts/set_override.py:95-130` (parser), `:300-340` (validation block), `:355-375` (credential verification block)
- Test: `tests/test_set_override_script.py`

**Interfaces:**
- Consumes: `model_check.problems(...)` (Task 2).
- Produces: a `--skip-probe` flag; exit code 2 on a failed probe.

- [ ] **Step 1: Write the failing tests**

Follow the existing test module's invocation style (`set_override.main([...])` with `store` monkeypatched).

```python
class TestModelProbe:
    def test_uncallable_model_refuses_and_writes_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(set_override.store, "init_pool", lambda: None)
        monkeypatch.setattr(set_override, "_refuse_local_test_db", lambda: False)
        monkeypatch.setattr(
            set_override._override, "verify_render_slot", lambda p, i: (True, "ok")
        )
        monkeypatch.setattr(set_override.store, "get_key_index_override", lambda p: 0)
        monkeypatch.setattr(set_override.store, "get_slot_config", lambda *a: {})

        def _never(*a, **k):
            raise AssertionError("must not write a refused model")

        monkeypatch.setattr(set_override.store, "set_slot_config", _never)
        monkeypatch.setattr(
            set_override.model_check, "problems", lambda *a, **k: ["model_not_callable"]
        )

        code = set_override.main(["vertex", "--model", "gemini-3.1-flash-lite"])

        assert code == 2
        assert "model_not_callable" in capsys.readouterr().err

    def test_skip_probe_writes_without_probing(self, monkeypatch, capsys):
        written = []
        monkeypatch.setattr(set_override.store, "init_pool", lambda: None)
        monkeypatch.setattr(set_override, "_refuse_local_test_db", lambda: False)
        monkeypatch.setattr(
            set_override._override, "verify_render_slot", lambda p, i: (True, "ok")
        )
        monkeypatch.setattr(set_override.store, "get_key_index_override", lambda p: 0)
        monkeypatch.setattr(set_override.store, "get_slot_config", lambda *a: {})
        monkeypatch.setattr(set_override.store, "set_provider_override", lambda *a: None)
        monkeypatch.setattr(
            set_override.store, "set_slot_config", lambda *a, **k: written.append(k)
        )

        def _never(*a, **k):
            raise AssertionError("--skip-probe must not probe")

        monkeypatch.setattr(set_override.model_check, "problems", _never)

        code = set_override.main(
            ["vertex", "--model", "gemini-2.5-flash", "--skip-probe"]
        )

        assert code == 0
        assert written
        assert "skipped" in capsys.readouterr().err.lower()

    def test_force_does_not_override_a_failed_probe(self, monkeypatch):
        """--force governs the credential-presence check only. A model that
        404s cannot be forced into production -- every review under it fails."""
        monkeypatch.setattr(set_override.store, "init_pool", lambda: None)
        monkeypatch.setattr(set_override, "_refuse_local_test_db", lambda: False)
        monkeypatch.setattr(
            set_override._override, "verify_render_slot", lambda p, i: (True, "ok")
        )
        monkeypatch.setattr(set_override.store, "get_key_index_override", lambda p: 0)
        monkeypatch.setattr(set_override.store, "get_slot_config", lambda *a: {})
        monkeypatch.setattr(
            set_override.model_check, "problems", lambda *a, **k: ["model_not_callable"]
        )

        assert set_override.main(
            ["vertex", "--model", "gemini-3.1-flash-lite", "--force"]
        ) == 2

    def test_activation_probes_the_target_slots_stored_model(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(set_override.store, "init_pool", lambda: None)
        monkeypatch.setattr(set_override, "_refuse_local_test_db", lambda: False)
        monkeypatch.setattr(
            set_override._override, "verify_render_slot", lambda p, i: (True, "ok")
        )
        monkeypatch.setattr(set_override.store, "get_key_index_override", lambda p: 0)
        monkeypatch.setattr(
            set_override.store, "get_slot_config", lambda *a: {"model": "stored-model"}
        )
        monkeypatch.setattr(set_override.store, "set_key_index_override", lambda *a: None)
        monkeypatch.setattr(set_override.store, "set_provider_override", lambda *a: None)
        monkeypatch.setattr(
            set_override.model_check,
            "problems",
            lambda provider, slot, model, **k: seen.update(
                {"provider": provider, "slot": slot, "model": model}
            ) or [],
        )

        assert set_override.main(["vertex", "--index", "1"]) == 0
        assert seen == {"provider": "vertex", "slot": 1, "model": "stored-model"}

    def test_model_only_with_no_activate_still_probes_the_new_model(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(set_override.store, "init_pool", lambda: None)
        monkeypatch.setattr(set_override, "_refuse_local_test_db", lambda: False)
        monkeypatch.setattr(set_override.store, "get_key_index_override", lambda p: 0)
        monkeypatch.setattr(set_override.store, "get_slot_config", lambda *a: {})
        monkeypatch.setattr(set_override.store, "set_slot_config", lambda *a, **k: None)
        monkeypatch.setattr(
            set_override.model_check,
            "problems",
            lambda provider, slot, model, **k: seen.update({"model": model}) or [],
        )

        assert set_override.main(
            ["vertex", "--model", "gemini-2.5-flash", "--no-activate"]
        ) == 0
        assert seen == {"model": "gemini-2.5-flash"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_set_override_script.py -k ModelProbe -v`
Expected: FAIL -- `AttributeError: module 'scripts.set_override' has no attribute 'model_check'`.

- [ ] **Step 3: Add the flag**

In `build_parser()`, next to `--force`:

```python
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help=(
            "do not make the live call that proves the model is actually "
            "callable (offline use, or a break-glass repair while the "
            "provider is unreachable)"
        ),
    )
```

`--force` is deliberately NOT extended to cover a failed probe: it means "write despite a failed credential-presence verification". A model that answers 404 makes every review fail, so there is nothing to force -- `--skip-probe` says "don't ask", which is an honest thing to want; "ask, be told no, proceed anyway" is not.

- [ ] **Step 4: Import and probe**

Add `model_check` to the providers import:

```python
from providers import active_model, model_check, pricing, registry
```

After the existing unpriced-model warning block (which stays exactly as it is -- see below), add:

```python
    # Two different rules, deliberately. An UNPRICED model runs fine and
    # simply produces no cost estimate, so it warns (above). An UNCALLABLE
    # model 404s every review, so it refuses. --skip-probe is the only way
    # past this, and it says what it skipped.
    if args.model is not None and not args.skip_probe:
        target_index = (
            args.index
            if args.index is not None
            else (0 if args.clear_index else (store.get_key_index_override(args.provider) or 0))
        )
        existing = store.get_slot_config(args.provider, target_index) or {}
        issues = model_check.problems(
            args.provider,
            target_index,
            stripped_model,
            vertex_gcp_project=existing.get("vertex_gcp_project"),
            vertex_gcp_location=existing.get("vertex_gcp_location"),
        )
        if issues:
            print(
                f"refusing to set the model: {args.provider} model "
                f"{stripped_model!r} failed its entitlement probe ({issues[0]}). "
                "Use --skip-probe to write it anyway.",
                file=sys.stderr,
            )
            return 2
    elif args.model is not None and args.skip_probe:
        print(
            f"warning: skipped the entitlement probe for {stripped_model!r} "
            "(--skip-probe) -- it has not been proven callable",
            file=sys.stderr,
        )
```

**This block must sit after `store.init_pool()`**, since it reads `slot_config`. Move it below the `store.init_pool()` call rather than leaving it with the argument validation above.

- [ ] **Step 5: Probe on activation**

Inside the existing `if not _credential_untouched:` block, after `verify_render_slot` succeeds and before the writes, add:

```python
        # Arming a slot makes ITS model live. The credential, project and
        # location all belong to that slot, so a model verified elsewhere
        # says nothing here -- and a row written before this gate existed is
        # caught right now rather than on the next PR.
        if not args.skip_probe and args.model is None:
            armed = store.get_slot_config(args.provider, effective_index) or {}
            arming_issues = model_check.problems(
                args.provider,
                effective_index,
                armed.get("model") or "",
                vertex_gcp_project=armed.get("vertex_gcp_project"),
                vertex_gcp_location=armed.get("vertex_gcp_location"),
            )
            if arming_issues:
                print(
                    f"refusing to activate {args.provider} slot {effective_index}: "
                    f"its configured model failed its entitlement probe "
                    f"({arming_issues[0]}). Set a working model first, or use "
                    "--skip-probe.",
                    file=sys.stderr,
                )
                return 2
```

`args.model is None` guards against double-probing: a call that sets a model AND activates has already probed that exact model in Step 4, against the same slot.

- [ ] **Step 6: Update the module docstring**

Its line 21-23 currently says `--model warns, rather than refuses...`. Extend that paragraph:

```
--model warns, rather than refuses, a value with no providers/pricing.py
rate-table entry for this provider -- naming the models that ARE known -- and
still sets the override: an unpriced model runs fine, it simply produces no
cost estimate. It DOES refuse a model that fails its live entitlement probe,
which is a different failure: an unpriced model works and reports no cost, an
unentitled one 404s every review. --skip-probe opts out of that live call.
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: PASS.

- [ ] **Step 8: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`

- [ ] **Step 9: Commit**

```bash
git add scripts/set_override.py tests/test_set_override_script.py
git commit -m "$(cat <<'EOF'
Refuse an unusable model from set_override, and probe on activation

The CLI is how production gets repaired, so leaving it unprobed kept the
incident reproducible through the very tool used to fix it. Distinct from the
existing pricing rule on purpose: unpriced warns (the model works, it just
reports no cost), uncallable refuses (every review 404s).

--skip-probe covers offline use and break-glass repair. --force is not
extended to cover this -- there is nothing to force about a model that
answers 404.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 6: Publish the rule in `contracts/provisioning.json`

**Files:**
- Modify: `scripts/gen_contract.py` (`CONTRACT_VERSION`, a new `model_validation()`, `build_contract()`)
- Modify: `contracts/provisioning.json` (regenerated, never hand-edited)
- Modify: `CLAUDE.md` (the cross-repo contract paragraph)
- Test: `tests/test_provisioning_contract.py`

**Interfaces:**
- Consumes: `registry.MODEL_PROBE_POLICY`, `registry.MODEL_PROBE_ERROR_CODES` (Task 1).
- Produces: contract block `model_validation: {"error_codes": [...], "providers": {<name>: {"required_before_write": bool, "mechanism": str | None, "reason": str | None}}}`; `contract_version == 2`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provisioning_contract.py`:

```python
class TestModelValidationBlock:
    def test_contract_version_is_two(self):
        assert gen_contract.CONTRACT_VERSION == 2

    def test_block_declares_every_provider(self):
        block = gen_contract.model_validation()

        assert set(block["providers"]) == set(registry.PROVIDERS)

    def test_vertex_and_gemini_require_a_probe_and_groq_explains_why_it_does_not(self):
        providers = gen_contract.model_validation()["providers"]

        assert providers["vertex"]["required_before_write"] is True
        assert providers["vertex"]["mechanism"] == "count_tokens"
        assert providers["gemini"]["required_before_write"] is True
        assert providers["groq"]["required_before_write"] is False
        assert providers["groq"]["reason"]

    def test_error_codes_are_published(self):
        assert gen_contract.model_validation()["error_codes"] == [
            "model_not_callable",
            "model_probe_unavailable",
        ]

    def test_block_carries_no_values_from_the_settings_instance(self):
        """Same constraint as every other block: names and non-secret policy
        only. A probe policy has no shape a credential could occupy, and this
        pins that it stays that way."""
        rendered = json.dumps(gen_contract.model_validation())

        assert "API_KEY" not in rendered
        assert "DATABASE_URL" not in rendered

    def test_committed_contract_is_in_sync(self):
        """The docs CI job byte-compares this; failing here first is friendlier."""
        committed = Path(gen_contract.CONTRACT_PATH).read_text()

        assert committed == gen_contract.render()
```

Add `from providers import registry` and `import json` / `from pathlib import Path` to that test module's imports if not already present.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: FAIL -- `AttributeError: module 'scripts.gen_contract' has no attribute 'model_validation'`.

- [ ] **Step 3: Generate the block**

In `scripts/gen_contract.py`, bump the version and add the function above `build_contract()`:

```python
CONTRACT_VERSION = 2
```

```python
def model_validation() -> dict[str, object]:
    """What makes a `model` value valid, not just present.

    slot_config's shape says a provisioner must write `model`; it has never
    said what a writable model IS. For Vertex that gap was load-bearing:
    client.models.list() returns the global Model Garden rather than a
    per-project entitlement list, so a provisioner filling a dropdown from it
    can write a model that 404s every review while reporting the choice as
    validated. That is not hypothetical -- it reached production, and it
    reached it through two independent implementations that had each inferred
    the same wrong thing from the same listing call.

    Published here rather than described in each repository's own docstrings
    because docstrings are exactly what was in place while that happened. The
    consumer vendors this file and asserts its own conformance against it.

    Read from registry.MODEL_PROBE_POLICY -- a module constant, like every
    other derivation in this file (see the module docstring's one rule).
    """
    return {
        "error_codes": list(registry.MODEL_PROBE_ERROR_CODES),
        "providers": {
            provider: dict(registry.MODEL_PROBE_POLICY[provider])
            for provider in sorted(registry.PROVIDERS)
        },
    }
```

And in `build_contract()`, between `providers` and `runtime_config`:

```python
        "providers": providers(),
        "model_validation": model_validation(),
        "runtime_config": runtime_config(),
```

- [ ] **Step 4: Regenerate the contract**

Run: `uv run python -m scripts.gen_contract`
Then: `git diff contracts/provisioning.json` -- confirm the diff is exactly the version bump plus the new block, and that no value from any credential appears.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Correct `CLAUDE.md`**

The cross-repo section states the contract "carries names, placements, SQL types and non-secret operational defaults only." That is now false, and leaving it is how the next reader concludes this block does not belong. Replace that sentence with:

```
It carries names, placements, SQL types, non-secret operational defaults, and
the validity rules for values only the provisioner can supply -- currently
`model_validation`, which declares that a model must be proven callable by a
live probe before it is written, because Vertex's own catalog listing is not
scoped by project entitlement and cannot answer that question. It reads the
`Settings` **class**'s declared defaults, never the module-level `settings`
instance, exactly as `gen_docs.py` does and for the same reason (see the
secret-handling section at the top of this file).
```

- [ ] **Step 7: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`

- [ ] **Step 8: Commit**

```bash
git add scripts/gen_contract.py contracts/provisioning.json tests/test_provisioning_contract.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Publish the model-validation rule in the provisioning contract

slot_config's shape said a provisioner must write a model; it never said what
a writable model is. For Vertex that gap was load-bearing, and both repos
that fill a model dropdown inferred the same wrong thing from the same
unscoped listing call -- independently, with near-identical docstrings. The
rule goes where the schema already goes, so the consumer asserts conformance
against a vendored declaration instead of a copied rationale.

A new block is a shape change: contract_version 1 -> 2. This also widens what
the contract is, from the row's shape to its shape and validity, so CLAUDE.md's
description of it is corrected in the same commit.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 7: The dashboard UI -- honest dropdown, honest gate

**Files:**
- Modify: `dashboard/static/dashboard.html` (i18n dictionaries; `rowVertexValidated`/`rowVertexBaseline` -> provider-agnostic; `renderSlotConfigRows`; `validateSlotConfigRow`)
- Test: `dashboard/tests/test_dashboard.py` (the module that asserts on the static page). `tests/test_config_field_registry.py` also parses this file's `CONFIG_FIELDS` block -- do not disturb that array's JSON-parseable shape.

**Interfaces:**
- Consumes: the error codes from Tasks 1/3/4 as they arrive in `result.error` / `failed[].error`.
- Produces: no new backend interface.

- [ ] **Step 1: Add the error strings to both language dictionaries**

Find the i18n object containing `no_credential_configured`'s neighbours and add to **each** language block (the file carries parallel dictionaries -- a string added to one only is a bug in the other):

```javascript
      err_model_not_callable: "This model is listed by the provider, but this project isn't entitled to call it. Pick a different model.",
      err_model_probe_unavailable: "Couldn't verify this model right now (provider unreachable). Try again.",
```

- [ ] **Step 2: Make the row-state maps provider-agnostic**

Rename `rowVertexValidated` -> `rowValidated` and `rowVertexBaseline` -> `rowBaseline` throughout, and record the model in the baseline. The maps were vertex-only because only vertex had anything per-row to validate; gemini is probed now too.

```javascript
    // Which rows have been live-confirmed for their CURRENTLY selected
    // model (and, for vertex, project+location) THIS PAGE LOAD -- but a
    // row's stored values are already known-good (they passed this same
    // check when they were saved), so Save's disabled state doesn't key off
    // this alone. Grows via a live "Validate" success, shrinks via a change
    // away from the stored baseline.
    const rowValidated = new Set();
    // Each row's values AS LAST SAVED -- lets a change back to what's
    // already on file re-enable Save without another live call.
    const rowBaseline = new Map();
```

In `renderSlotConfigRows`, set the baseline for **every** provider, not just vertex:

```javascript
          rowBaseline.set(rowKey, {
            model: entry.model || "",
            project: entry.vertex_gcp_project || "",
            location: entry.vertex_gcp_location || "",
          });
```

- [ ] **Step 3: Give gemini rows a Validate button**

The Validate button currently lives inside the `if (provider === "vertex")` block. Move it out so it renders for any provider whose model is probed, and leave groq without one:

```javascript
          const probed = provider === "vertex" || provider === "gemini";
          const validateButton = probed
            ? `<button type="button" class="control slot-config-validate" data-provider="${esc(provider)}" data-slot="${slot}">${t("env_config_validate")}</button>`
            : "";
```

Render `${vertexFields}${validateButton}` in place of the old `${vertexFields}`, with the button removed from `vertexFields` itself.

- [ ] **Step 4: Re-arm the gate on a model change**

Extend the change listener to cover the model select. Its current selector is `'select[id$="-project"], select[id$="-location"]'`:

```javascript
      container.querySelectorAll('select[id$="-model"], select[id$="-project"], select[id$="-location"]').forEach((select) => {
        select.addEventListener("change", () => {
          // data-provider/data-slot live on the project/location selects
          // already; the model select needs them too (Step 5).
          const provider = select.getAttribute("data-provider");
          const slot = select.getAttribute("data-slot");
          if (provider === "groq") return;  // nothing to validate
          const rowKey = `${provider}-${slot}`;
          const rowId = `slotCfg-${provider}-${slot}`;
          const saveBtn = container.querySelector(`.slot-config-save[data-provider="${provider}"][data-slot="${slot}"]`);
          const hint = document.querySelector(`[data-slot-result="${provider}-${slot}"]`);
          const baseline = rowBaseline.get(rowKey) || {};
          const projectEl = document.getElementById(`${rowId}-project`);
          const locationEl = document.getElementById(`${rowId}-location`);
          const matchesBaseline =
            document.getElementById(`${rowId}-model`).value === (baseline.model || "")
            && (!projectEl || projectEl.value === (baseline.project || ""))
            && (!locationEl || locationEl.value === (baseline.location || ""));
          if (matchesBaseline) {
            if (saveBtn) saveBtn.disabled = false;
            if (hint) hint.textContent = "";
            return;
          }
          rowValidated.delete(rowKey);
          if (saveBtn) saveBtn.disabled = true;
          if (hint) hint.textContent = t("env_config_needs_revalidate");
        });
      });
```

- [ ] **Step 5: Carry `data-provider`/`data-slot` on the model select**

```javascript
              <select id="${rowId}-model" data-provider="${esc(provider)}" data-slot="${slot}">${modelOptions}</select>
```

- [ ] **Step 6: Make `validateSlotConfigRow` check the model**

Send the selected model so the backend's `_validate_model_var` probes it, and map the returned code through the i18n table rather than printing it raw:

```javascript
        const resp = await fetch(`/api/environment/validate/${MODEL_VAR_BY_PROVIDER[provider]}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: document.getElementById(`${rowId}-model`).value }),
        });
```

with, near the other module-level constants:

```javascript
    const MODEL_VAR_BY_PROVIDER = {
      gemini: "GEMINI_MODEL",
      groq: "GROQ_MODEL",
      vertex: "VERTEX_MODEL",
    };
```

Keep the existing `/credential/{provider}/models` call for vertex rows -- it is what refreshes the project list and checks the project/location pair. The model validate is an additional call on the same click, not a replacement: a vertex row needs both answers, and `_validate_model_var` reads the slot's STORED project/location, so the pair must be saved or confirmed separately. Report the first failure of the two, mapping the code:

```javascript
          const message = t(`err_${result.error}`) || result.error;
          if (hint) hint.textContent = `${t("env_invalid")}: ${message}`;
```

- [ ] **Step 7: Label the dropdown honestly**

Where the model dropdown's hint/label text is rendered, add a short note for probed providers so the list does not imply verification it has not obtained:

```javascript
      env_config_model_unverified_hint: "Listed by the provider — verified when you validate or save.",
```

Render it as a `field-hint` beside the model select for `probed` rows.

- [ ] **Step 8: Run the dashboard tests**

Run: `uv run pytest dashboard/tests -v`
Expected: PASS. Update any test asserting on `rowVertexValidated`/`rowVertexBaseline` by name.

- [ ] **Step 9: Visual review (REQUIRED)**

Invoke the `ui-visual-review` skill against the Environment tab's config panel. Root `CLAUDE.md` requires it for any `dashboard/static/` change: reading the HTML is not a substitute for rendering it. Confirm at light-desktop, dark-desktop and mobile that the gemini rows' new Validate button does not overflow the row and the new hint does not push the Save button off the line.

- [ ] **Step 10: Full suite and lint**

Run: `uv run pytest -v -n 4` then `uv run ruff check .`

- [ ] **Step 11: Commit**

```bash
git add dashboard/static/dashboard.html dashboard/tests
git commit -m "$(cat <<'EOF'
Put the model inside the config panel's own validate gate

The per-row gate tracked project+location only, and validateSlotConfigRow
never checked the model at all -- there was no path through this UI on which
the chosen model was verified against anything, which is how a listed but
uncallable model was saved from an otherwise-valid row.

The model now re-arms the gate like the other two fields, gemini rows get the
same Validate control (its model is probed too), and groq keeps none since
there is nothing to check. The dropdown says what the listing actually means.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

### Task 8: The consumer half -- `~/onboarding-wizard`

**This task is in a different repository.** Complete Tasks 1-7 first and confirm this repo is green: the contract lands here on its own, and the consumer catches up afterwards (`CLAUDE.md`'s cross-repo ownership direction -- never block on the consumer).

**Files:**
- Modify: `~/onboarding-wizard/llm_client.py` (`_list_generative_models`'s docstring; new probes)
- Modify: `~/onboarding-wizard/router.py:303-380` (`_seed_provider_config`), `:893-955` (its caller)
- Modify: `~/onboarding-wizard/static/index.html` (both language dictionaries)
- Modify: `~/onboarding-wizard/contracts/provisioning.json` (vendored via that repo's `scripts/update_bot_contract.py`)
- Test: `~/onboarding-wizard/tests/`

**Interfaces:**
- Consumes: the vendored `contracts/provisioning.json`'s `model_validation` block (Task 6).
- Produces:
  - `llm_client.LlmModelProbed` -- new frozen dataclass, one field `model: str`, mirroring `LlmModelsListed`'s shape
  - `llm_client.probe_vertex_model(service_account_key_b64: str, model: str, project: str | None = None, location: str | None = None) -> LlmModelProbed | LlmApiFailed`
  - `llm_client.probe_gemini_model(api_key: str, model: str) -> LlmModelProbed | LlmApiFailed`
  - `llm_client.MODEL_PROBE_ERROR_CODES: tuple[str, str]`

- [ ] **Step 1: Vendor the new contract**

```bash
cd ~/onboarding-wizard && uv run python -m scripts.update_bot_contract
git diff contracts/provisioning.json
```
Expected: `contract_version` 2 and the `model_validation` block appear. (If that script's name differs, find it with `ls scripts/`.)

- [ ] **Step 2: Write the conformance test first**

Create `~/onboarding-wizard/tests/test_model_validation_conformance.py`:

```python
"""The contract declares which providers need a live entitlement probe before
a model is written; this asserts THIS repo implements exactly that.

Without this test the contract documents the rule and nothing checks we obey
it -- which is the whole difference between a vendored declaration and a
docstring. See pr-review-bot's docs/superpowers/specs/2026-09-11-vertex-model-
entitlement-validation-design.md section 7c.
"""
from __future__ import annotations

import json
from pathlib import Path

import llm_client

CONTRACT = json.loads(Path("contracts/provisioning.json").read_text())


def test_contract_version_is_understood():
    assert CONTRACT["contract_version"] == 2


def test_a_probe_exists_for_every_provider_that_requires_one():
    for provider, policy in CONTRACT["model_validation"]["providers"].items():
        probe = getattr(llm_client, f"probe_{provider}_model", None)
        if policy["required_before_write"]:
            assert callable(probe), f"{provider} requires a probe and none is implemented"
        else:
            assert probe is None, f"{provider} requires no probe but one exists"


def test_reported_codes_are_exactly_the_declared_ones():
    assert set(llm_client.MODEL_PROBE_ERROR_CODES) == set(
        CONTRACT["model_validation"]["error_codes"]
    )
```

- [ ] **Step 3: Run it to verify failure**

Run: `cd ~/onboarding-wizard && uv run pytest tests/test_model_validation_conformance.py -v`
Expected: FAIL -- `probe_vertex_model` does not exist.

- [ ] **Step 4: Write the probe tests**

`tests/test_onboarding_llm_client.py` already mocks at the SDK client boundary (`_FakeClient` records constructor kwargs, `_FakeModelsResource` serves a fake pager) and patches `service_account.Credentials.refresh` to a no-op so the throwaway sentinel key never reaches Google's token endpoint. Extend that fixture -- do not build a second one.

First teach `_FakeModelsResource` to answer a probe. Add to that class:

```python
    async def count_tokens(self, **kwargs):
        self.count_tokens_kwargs = kwargs
        if self._exc:
            raise self._exc
        return SimpleNamespace(total_tokens=1)
```

Then append these tests to the same module (it already imports `genai_errors` and `llm_client`, and defines `_b64`, `_install_fake_client`, `_FakeClient` and `_SENTINEL_SERVICE_ACCOUNT`):

```python
async def test_probe_vertex_model_returns_ok_for_a_callable_model(monkeypatch):
    _install_fake_client(monkeypatch)
    result = await llm_client.probe_vertex_model(
        _b64(_SENTINEL_SERVICE_ACCOUNT), "gemini-2.5-flash"
    )
    assert result == llm_client.LlmModelProbed(model="gemini-2.5-flash")


async def test_probe_vertex_model_404_is_model_not_callable(monkeypatch):
    """The production failure exactly: the model IS in the listing and a real
    call still 404s, because Vertex's listing is not entitlement-scoped."""
    _install_fake_client(
        monkeypatch,
        exc=genai_errors.ClientError(404, {"message": "Publisher model not found"}),
    )
    result = await llm_client.probe_vertex_model(
        _b64(_SENTINEL_SERVICE_ACCOUNT), "gemini-3.1-flash-lite"
    )
    assert result == llm_client.LlmApiFailed(reason="model_not_callable")


async def test_probe_vertex_model_rate_limited_is_probe_unavailable(monkeypatch):
    """No verdict was reached. Reporting that as an unusable model would send
    a visitor hunting for a replacement that was never the problem."""
    _install_fake_client(
        monkeypatch, exc=genai_errors.ClientError(429, {"message": "slow down"})
    )
    result = await llm_client.probe_vertex_model(
        _b64(_SENTINEL_SERVICE_ACCOUNT), "gemini-2.5-flash"
    )
    assert result == llm_client.LlmApiFailed(reason="model_probe_unavailable")


async def test_probe_vertex_model_forbidden_stays_a_credential_verdict(monkeypatch):
    _install_fake_client(
        monkeypatch, exc=genai_errors.ClientError(403, {"message": "no role"})
    )
    result = await llm_client.probe_vertex_model(
        _b64(_SENTINEL_SERVICE_ACCOUNT), "gemini-2.5-flash"
    )
    assert result == llm_client.LlmApiFailed(reason="forbidden")


async def test_probe_vertex_model_rejects_an_unpinned_token_uri(monkeypatch):
    """The same SSRF guard list_vertex_models carries -- the visitor supplies
    this key, so token_uri must never be honoured. See ISSUES.md's entry."""
    _install_fake_client(monkeypatch)
    hostile = {**_SENTINEL_SERVICE_ACCOUNT, "token_uri": "http://169.254.169.254/"}
    result = await llm_client.probe_vertex_model(_b64(hostile), "gemini-2.5-flash")
    assert result == llm_client.LlmApiFailed(reason="invalid_service_account_json")


async def test_probe_vertex_model_closes_the_client_on_failure(monkeypatch):
    _install_fake_client(
        monkeypatch, exc=genai_errors.ClientError(404, {"message": "nope"})
    )
    await llm_client.probe_vertex_model(_b64(_SENTINEL_SERVICE_ACCOUNT), "m")
    assert _FakeClient.last_instance.aio.closed is True


async def test_probe_vertex_model_uses_the_given_project_and_location(monkeypatch):
    _install_fake_client(monkeypatch)
    await llm_client.probe_vertex_model(
        _b64(_SENTINEL_SERVICE_ACCOUNT),
        "gemini-2.5-flash",
        project="other-project",
        location="europe-west4",
    )
    assert _FakeClient.last_kwargs["project"] == "other-project"
    assert _FakeClient.last_kwargs["location"] == "europe-west4"


async def test_probe_gemini_model_404_is_model_not_callable(monkeypatch):
    _install_fake_client(
        monkeypatch, exc=genai_errors.ClientError(404, {"message": "nope"})
    )
    result = await llm_client.probe_gemini_model("sentinel-api-key", "no-such-model")
    assert result == llm_client.LlmApiFailed(reason="model_not_callable")


async def test_probe_gemini_model_returns_ok(monkeypatch):
    _install_fake_client(monkeypatch)
    result = await llm_client.probe_gemini_model(
        "sentinel-api-key", "gemini-flash-latest"
    )
    assert result == llm_client.LlmModelProbed(model="gemini-flash-latest")
```

- [ ] **Step 5: Implement the probes**

In `llm_client.py`, mirroring the existing `list_vertex_models` (same SSRF guard on `token_uri`/`universe_domain` before `from_service_account_info`, same `aio` style):

```python
@dataclasses.dataclass(frozen=True)
class LlmModelProbed:
    model: str


MODEL_PROBE_ERROR_CODES = ("model_not_callable", "model_probe_unavailable")

_PROBE_CONTENTS = "ping"


def _probe_reason(code: int) -> str:
    """404 means this project may not call the model -- the same answer a real
    generateContent gives. Anything else means no verdict was reached, which
    is NOT the same answer."""
    if code == 404:
        return "model_not_callable"
    if code in (401, 403):
        return _reason_for_client_error_code(code)
    return "model_probe_unavailable"
```

Then `probe_vertex_model` / `probe_gemini_model` calling `client.aio.models.count_tokens(model=model, contents=_PROBE_CONTENTS)` and classifying via `_probe_reason`, returning `LlmApiFailed(reason=...)` on failure. **The SSRF guard is not optional** -- a visitor supplies this key, and ISSUES.md's entry on `list_vertex_models` is what happens without it.

- [ ] **Step 6: Correct `_list_generative_models`'s docstring**

Its current text explains why nothing is filtered out and stops there. Append:

```
WHAT THIS LIST IS NOT: for Vertex it is essentially the global Model Garden,
not a per-project entitlement list -- a live listing returns veo-*, lyria-*
and medgemma entries no ordinary project can call. Membership means the
credential authenticates, nothing more. Whether the project may actually
generate with a model is probe_vertex_model's question. Treating the two as
one fact put a 404-ing model into a provisioned deployment.
```

- [ ] **Step 7: Gate `_seed_provider_config`'s caller**

The probe is async and `_seed_provider_config` is sync (it runs under `asyncio.to_thread`), so the probe belongs in the async caller, **before** the Render credential push and before the seed call -- same ordering rule as this project's `_apply_llm_credential`. On failure, return the existing failure shape with the probe's reason:

```python
    probe = await llm_client.probe_vertex_model(key_b64, model, project, location)
    if isinstance(probe, LlmApiFailed):
        return {"valid": False, "reason": probe.reason, "pushed": []}
```

Add a test asserting the seed function is never reached when the probe fails.

- [ ] **Step 8: Add the error strings to BOTH locale dictionaries**

Beside `err_llm_no_models_available` (:1311 English, :1464 Hebrew):

```javascript
      err_model_not_callable: "This model is listed by Google, but your project isn't allowed to use it. Please choose a different model.",
      err_model_probe_unavailable: "We couldn't verify this model right now. Please try again.",
```

Hebrew equivalents go in the second dictionary. Add a test that both dictionaries carry identical key sets.

- [ ] **Step 9: Run that repo's full suite and lint**

Run: `cd ~/onboarding-wizard && uv run pytest -v && uv run ruff check .`

- [ ] **Step 10: Commit in the wizard repo**

```bash
cd ~/onboarding-wizard
git add -A
git commit -m "$(cat <<'EOF'
Probe model entitlement before provisioning writes it to slot_config

Vertex's model listing is the global Model Garden, not a per-project
entitlement list, so the dropdown this wizard fills from it could offer -- and
seed into a fresh deployment -- a model that 404s every review. countTokens
answers the real question for free.

The rule comes from the vendored provisioning contract rather than a copied
docstring, and a conformance test asserts this repo implements exactly what
the contract declares.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Te7UaGBA1aUzcSEdnupdek
EOF
)"
```

---

## Done criteria

- `uv run pytest -v` and `uv run ruff check .` green in both repos.
- `uv run python -m scripts.gen_contract` produces no diff (the CI `docs` job byte-compares it).
- `deploy-verify` skill run before any push to `main`.
- `ui-visual-review` skill run against Task 7.
- Every parked/deferred Minor finding from review logged in `ISSUES.md`'s Parked Issues before the branch is considered done (root `CLAUDE.md`).
