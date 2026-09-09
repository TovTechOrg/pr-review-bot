# Provider/key-index DB-only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `runtime_config.provider`/`runtime_config.{provider}_key_index` the sole source of truth for provider selection (no `LLM_PROVIDER` env var anywhere), with a boot-time check that refuses to start unless a valid `slot_config` row backs the active provider/index, and have the onboarding wizard seed that state correctly.

**Architecture:** Remove `Settings.llm_provider` and every read of it; `providers/active.py::active_provider()` becomes a pure DB-cache read with no fallback; `main.py`'s lifespan validates the DB state directly instead of an env var. `scripts/deploy.py`/`doctor.py`'s local-`.env.config`-driven provider checks collapse into DB-only checks (the DB is required, `scripts/set_override.py` is how an operator sets it — already exists, unaffected). The onboarding wizard writes `runtime_config.provider`/`_key_index` alongside its existing `slot_config` seed.

**Tech Stack:** Python, FastAPI, psycopg (raw connections for one-shot CLI/wizard writes; pooled `ConnectionPool` for the running app), pytest, ruff.

**Spec:** `pr-review-bot/docs/superpowers/specs/2026-09-09-provider-key-index-db-only-design.md`

## Global Constraints

- No migration path for existing deployments — none are in production (per spec background). Hard cutover.
- Credentials (`GEMINI_API_KEY`, `GROQ_API_KEY`, `VERTEX_GCP_SERVICE_ACCOUNT_KEY`[+slots]) stay env-var-only, never DB-stored (spec section 8) — untouched by this plan.
- `providers/key_index.py`'s implicit index-0 default is unchanged (spec section 3) — only its *validity* (a matching `slot_config` row) is now checked at boot.
- `slot_config`'s `vertex_gcp_project`/`vertex_gcp_location` columns are unchanged (spec section 6, decided).
- Run `uv run pytest -v` and `uv run ruff check .` in each repo before considering that repo's work done; fix whatever either finds.

---

### Task 1: pr-review-bot — remove `Settings.llm_provider`, rewrite `active_provider()`, DB-backed boot check

**Files:**
- Modify: `config.py` (remove `llm_provider` field, ~line 97-104)
- Modify: `providers/active.py` (rewrite `active_provider()`)
- Modify: `main.py` (rewrite lifespan's provider check, ~line 39-44)
- Modify: `render.yaml` (remove `LLM_PROVIDER` entry, ~line 38)
- Test: `tests/test_main.py` (or wherever lifespan is tested), `tests/test_active.py` (or wherever `providers/active.py` is tested — find via `grep -rl "active_provider\|providers.active" tests/`)

**Interfaces:**
- Consumes: `review_queue.store` (already has `get_provider_override`/no direct "get slot config for provider+index" helper — use `store.get_slot_config(provider, index)`, existing), `providers.key_index.active_key_index`, `providers.registry.PROVIDERS`.
- Produces: `providers.active.active_provider() -> str` (raises nothing itself; returns `""` when the cache is empty — same "empty string means unconfigured" shape `config.py`'s old field used, so `main.py`'s `not in registry.PROVIDERS` check still works unmodified against the return value). `providers.active.set_override_cache`/`reset_override_cache` unchanged.

- [ ] **Step 1: Remove the `llm_provider` field from `config.py`**

Delete these lines (and the comment block immediately above them, "No implicit default: guessing a provider means silently running..."):
```python
    llm_provider: str = ""
```
Leave `gemini_model`, `gemini_api_key`, etc. untouched.

- [ ] **Step 2: Rewrite `providers/active.py`**

```python
"""The provider actually in force: read from the DB-refresh cache, no env
fallback (see docs/superpowers/specs/2026-09-09-provider-key-index-db-only-
design.md). Generalizes the 2026-09-08 slotted-config work's DB-only
treatment (already applied to model) to provider selection itself.

Every read of the active provider goes through active_provider(). Partial
adoption would be a bug -- if only the dispatcher consulted the cache, the
factory would still build whatever the cache was empty for, gating on one
provider while calling another.

This module deliberately imports nothing DB-related: the DB read lives in
the dispatcher (where the asyncio.to_thread convention applies) and is
pushed in via set_override_cache. That keeps webhook.py from pulling the DB
driver in through this import, and keeps active_provider() non-blocking.

An empty cache (before the first refresh, or the DB row is NULL) returns
"" -- a real "unconfigured" state, not a default to silently run. The
caller (main.py's lifespan) is responsible for turning that into a boot
failure.
"""

from __future__ import annotations

_override: str = ""


def active_provider() -> str:
    return _override


def set_override_cache(value: str | None) -> None:
    global _override
    _override = value or ""


def reset_override_cache() -> None:
    set_override_cache("")
```

- [ ] **Step 3: Rewrite `main.py`'s lifespan provider check**

Replace:
```python
    if settings.llm_provider not in registry.PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER={settings.llm_provider!r} is not a supported provider "
            f"-- refusing to start. Set it in .env.config to one of: "
            f"{', '.join(sorted(registry.PROVIDERS))}."
        )
```
with:
```python
    _provider = store.get_provider_override()
    if _provider not in registry.PROVIDERS:
        raise RuntimeError(
            f"runtime_config.provider={_provider!r} is not a supported provider "
            f"-- refusing to start. Set it with "
            f"`uv run python -m scripts.set_override <provider>` to one of: "
            f"{', '.join(sorted(registry.PROVIDERS))}."
        )
    _index = key_index.active_key_index(_provider)
    if store.get_slot_config(_provider, _index) is None:
        raise RuntimeError(
            f"no slot_config row for provider={_provider!r} index={_index} "
            f"-- refusing to start. Configure a model with "
            f"`uv run python -m scripts.set_override {_provider} --model <name>`."
        )
```
This runs before `store.init_pool()` is called elsewhere in the lifespan, so confirm `store.init_pool()` (or an earlier line) already runs first — `store.get_provider_override()`/`get_slot_config()` need the pool open. Check `main.py`'s existing lifespan ordering (`grep -n "init_pool\|def lifespan" main.py`) and place this block *after* `store.init_pool()`, not before — reorder the existing checks if needed so DB-dependent checks come after the pool opens, and non-DB checks (webhook secret, dashboard vars) can stay wherever they are relative to it. Add the two now-needed imports at the top of `main.py`: `from review_queue import store` and `from providers import key_index` (check they aren't already imported under different names first — `grep -n "^from review_queue\|^from providers\|^import" main.py`).

- [ ] **Step 4: Remove `LLM_PROVIDER` from `render.yaml`**

Delete the `- key: LLM_PROVIDER` block (line 38 and its following `sync: false`/value lines, whatever the entry's full shape is — read the surrounding 5 lines first with `sed -n '30,45p' render.yaml` to get the exact block boundaries before deleting).

- [ ] **Step 5: Update tests**

Find every test that sets `settings.llm_provider` or monkeypatches `providers.active._override`/`config.settings.llm_provider`:
```bash
grep -rln "llm_provider" tests/
```
For each hit: replace `settings.llm_provider = "groq"`-style fixtures with `providers.active.set_override_cache("groq")` (for tests of `active_provider()`/factory/dispatcher behavior), or with seeding `runtime_config.provider` directly via `store.set_provider_override("groq", now)` (for tests that exercise `main.py`'s lifespan or `scripts/deploy.py` against a real/fake DB). Add a new test asserting `main.py`'s lifespan raises when `runtime_config.provider` is NULL, and a new test asserting it raises when `slot_config` has no row for the active `(provider, index)`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest -v -k "lifespan or active_provider or provider"`
Expected: PASS (after fixing whatever the rewritten fixtures need)

- [ ] **Step 7: Commit**

```bash
cd /home/emanresu/pr-review-bot
git add config.py providers/active.py main.py render.yaml tests/
git commit -m "Make provider selection DB-only; drop LLM_PROVIDER

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HjWY8hGxwRpQQqcKwZK2wp"
```

---

### Task 2: pr-review-bot — `scripts/deploy.py`: collapse env/DB dual-source into DB-only

**Files:**
- Modify: `scripts/deploy.py` (`_resolved_provider`, delete `_resolved_provider_or_env`, `check_provider`, `check_provider_live`, `check_api_key_live`, `check_config`'s LLM_PROVIDER block ~line 279-291, `_BOOT_CREDENTIAL_NAMES` ~line 362-370, `_wanted_env` ~line 1033-1063, `sync_env` ~line 1470-1495 and its `_wanted_env()`/`_seed_slot_zero_config_if_missing()` call sites ~line 1516/1543, `_seed_slot_zero_config_if_missing` ~line 1394-1420, CheckSpec description text ~line 1693)
- Test: `tests/test_deploy.py` (or wherever these functions are tested — `grep -rl "_resolved_provider\|check_provider\|_wanted_env\|sync_env" tests/`)

**Interfaces:**
- Consumes: `providers.registry.PROVIDERS`, `providers.registry.KEY_INDEX_COLUMNS`, `config.settings` (everything except `llm_provider`, which is gone as of Task 1).
- Produces: `_resolved_provider() -> str` (raises `RuntimeError` if `runtime_config.provider` is NULL; callers must confirm `settings.database_url` is set first — a psycopg connection error propagates as-is for the caller's own `except Exception` to catch). `_wanted_env(provider: str) -> dict[str, str]` (now takes the resolved provider as a parameter instead of reading `settings.llm_provider` internally). `_seed_slot_zero_config_if_missing(provider: str) -> None` (same — takes provider as a parameter).

- [ ] **Step 1: Rewrite `_resolved_provider`, delete `_resolved_provider_or_env`**

Replace (current lines ~648-666, docstring + body):
```python
def _resolved_provider() -> tuple[str, str | None]:
    """(active provider, override or None). ..."""
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    override = (row[0] if row else None) or None
    return (override or settings.llm_provider), override
```
with:
```python
def _resolved_provider() -> str:
    """The active provider, from runtime_config.provider -- required, no env
    fallback (docs/superpowers/specs/2026-09-09-provider-key-index-db-only-
    design.md). Raises RuntimeError if the row's provider is NULL. Callers
    must confirm settings.database_url is set before calling -- a bare
    psycopg.connect against an empty DSN raises on its own, which every
    caller already treats as a DB-unreachable condition via `except
    Exception`.
    """
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    provider = (row[0] if row else None) or None
    if provider is None:
        raise RuntimeError(
            "runtime_config.provider is unset -- run "
            "`uv run python -m scripts.set_override <provider>` first"
        )
    return provider
```
Delete `_resolved_provider_or_env` entirely (current lines ~735-739).

- [ ] **Step 2: Rewrite `check_provider`**

```python
def check_provider() -> CheckResult:
    """Which provider will actually run, and whether its credential exists."""
    name = "provider"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to resolve the active provider")
    try:
        provider = _resolved_provider()
    except RuntimeError as exc:
        return CheckResult(name, "FAIL", str(exc))
    except Exception as exc:  # noqa: BLE001 -- deliberate: a DB problem is database's row to report, not ours
        return CheckResult(name, "SKIPPED", f"could not read runtime_config ({type(exc).__name__})")
    entry = _PROVIDERS.get(provider)
    if entry is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        return CheckResult(name, "FAIL", f"{provider} is not supported (expected: {accepted})")
    credential = entry[0]
    if not getattr(settings, credential.lower(), ""):
        return CheckResult(name, "FAIL", f"{provider} -- {credential} missing")
    return CheckResult(name, "PASS", provider)
```

- [ ] **Step 3: Rewrite `check_provider_live`**

Same shape as `check_provider` for provider resolution, keep the rest (Render service lookup, credential-presence-on-Render check) unchanged except dropping the now-gone `source` string (just use `provider` directly in messages):
```python
def check_provider_live() -> CheckResult:
    """Whether the actively-resolved provider's credential is genuinely
    present on the live Render service -- not just locally."""
    name = "provider-live"
    if not settings.render_api_key:
        return CheckResult(
            name, "FAIL", "RENDER_API_KEY is required -- set it to verify credentials "
            "against the live service"
        )
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to resolve the active provider")
    try:
        provider = _resolved_provider()
    except RuntimeError as exc:
        return CheckResult(name, "FAIL", str(exc))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active provider ({type(exc).__name__})"
        )
    entry = _PROVIDERS.get(provider)
    if entry is None:
        return CheckResult(name, "SKIPPED", f"{provider} is not a supported provider")
    credential = entry[0]
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        live_value = _render.env_vars(service_id).get(credential) or ""
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not live_value:
        return CheckResult(name, "FAIL", f"{provider} -- {credential} not present on Render")
    return CheckResult(name, "PASS", f"{provider} -- {credential} present on Render")
```

- [ ] **Step 4: Fix `check_api_key_live`'s provider resolution**

Replace:
```python
    try:
        provider, _provider_override = _resolved_provider_or_env()
    # deliberate: a DB problem is provider's/database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
```
with:
```python
    try:
        provider = _resolved_provider()
    # deliberate: a DB problem is provider's/database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
```
(the rest of the function is unchanged — it never used `_provider_override`).

- [ ] **Step 5: Delete `check_config`'s LLM_PROVIDER block**

Remove (current lines ~279-291):
```python
    if not settings.llm_provider:
        problems.append(
            "LLM_PROVIDER is unset -- there is no default. Set it in .env.config "
            f"to one of: {', '.join(sorted(_PROVIDERS))}"
        )
    elif (entry := _PROVIDERS.get(settings.llm_provider)) is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        problems.append(
            f"LLM_PROVIDER={settings.llm_provider!r} is not supported "
            f"(expected one of: {accepted})"
        )
    else:
        credential = entry[0]
        if not getattr(settings, credential.lower(), ""):
            missing.append(credential)
```
There is no replacement — provider validity is entirely `check_provider`'s job now; `check_config` no longer has a local, env-based notion of "provider" to check at all.

- [ ] **Step 6: Remove `LLM_PROVIDER` from `_BOOT_CREDENTIAL_NAMES`**

Delete the `"LLM_PROVIDER",` line from the tuple, and update the comment above it (currently: "LLM_PROVIDER (must be a supported provider) and GITHUB_WEBHOOK_SECRET (must be non-empty) checked directly") to drop the LLM_PROVIDER clause, since `main.py`'s lifespan now checks it via the DB, not this env-var list.

- [ ] **Step 7: Rewrite `_wanted_env` to take `provider` as a parameter**

```python
def _wanted_env(provider: str) -> dict[str, str]:
    """Local values for every var --sync-env will push, for the given
    active provider (resolved by the caller via _resolved_provider()).

    Keys: the always-synced vars, plus every provider's credential (the
    active one always, the others only when they have a local value -- an
    opt-in .env lists the others empty, and must never be asked to fill
    them). No LLM_PROVIDER, no model var: both are DB-only now.
    """
    wanted = {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_INSTALLATION_ID": str(settings.github_app_installation_id or ""),
        "GITHUB_APP_PRIVATE_KEY": settings.github_app_private_key,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "DASHBOARD_USERNAME": settings.dashboard_username,
        "DASHBOARD_PASSWORD": settings.dashboard_password,
        "DASHBOARD_SESSION_SECRET": settings.dashboard_session_secret,
        "RENDER_API_KEY": settings.render_api_key,
    }
    entry = _PROVIDERS.get(provider)
    if entry is not None:
        credential, _ = entry
        wanted[credential] = getattr(settings, credential.lower(), "")
    for other_credential, _model_var in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
    for credential, _ in _PROVIDERS.values():
        wanted.update(_override.local_slot_values(credential))
    for env_name, attr in _GENERIC_OPERATIONAL_ENV_ATTRS.items():
        wanted[env_name] = str(getattr(settings, attr))
    return wanted
```
(Copy the exact trailing `_GENERIC_OPERATIONAL_ENV_ATTRS` loop and anything else after it from the current function body — read `sed -n '1033,1075p' scripts/deploy.py` right before editing to confirm nothing else follows that this rewrite would drop.)

- [ ] **Step 8: Rewrite `_seed_slot_zero_config_if_missing` to take `provider` as a parameter**

Replace the line `provider = settings.llm_provider` (and the `if provider not in _PROVIDERS: return` immediately after — keep that guard, just against the parameter now) with a `provider: str` parameter:
```python
def _seed_slot_zero_config_if_missing(provider: str) -> None:
    """... (docstring unchanged) ..."""
    if provider not in _PROVIDERS:
        return
    model_var = _PROVIDERS[provider][1]
    ...  # rest of the function body unchanged
```

- [ ] **Step 9: Wire `sync_env()` to resolve `provider` once and thread it through**

Replace the current guard block:
```python
    if settings.llm_provider not in _PROVIDERS:
        accepted = ", ".join(sorted(_PROVIDERS))
        print(
            f"refusing to sync LLM_PROVIDER={settings.llm_provider!r}: "
            f"not a supported provider (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if settings.database_url:
        try:
            _, override = _resolved_provider()
        # deliberate: the provider check reports DB trouble
        except Exception:  # noqa: BLE001
            override = None
        if override and override != settings.llm_provider:
            print(
                f"refusing to sync: a DB provider override ({override}) is active and "
                f"wins over the LLM_PROVIDER={settings.llm_provider} being pushed. "
                "Clear it first: uv run python -m scripts.set_override --clear",
                file=sys.stderr,
            )
            return 2
        # The former model-override-disagreement guard ... (comment, keep or drop, see below)
```
with:
```python
    if not settings.database_url:
        print(
            "refusing to sync: DATABASE_URL is required to resolve the active "
            "provider from runtime_config",
            file=sys.stderr,
        )
        return 2
    try:
        provider = _resolved_provider()
    except RuntimeError as exc:
        print(f"refusing to sync: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- deliberate: the provider check reports DB trouble
        print(f"refusing to sync: could not resolve the active provider ({type(exc).__name__})", file=sys.stderr)
        return 2
```
(Drop the "former model-override-disagreement guard" comment entirely — it was explaining why an *old* guard doesn't apply; with the whole block replaced it has nothing left to attach to.)

Then update the two call sites later in the same function:
```python
    wanted = _wanted_env()
```
→
```python
    wanted = _wanted_env(provider)
```
and:
```python
        try:
            _seed_slot_zero_config_if_missing()
        except Exception as exc:  # noqa: BLE001
```
→
```python
        try:
            _seed_slot_zero_config_if_missing(provider)
        except Exception as exc:  # noqa: BLE001
```
(the `if settings.database_url:` guard around this second call site can now be deleted too, since `sync_env()` already refused to proceed above when `settings.database_url` was falsy — dedent the block's body to top level).

- [ ] **Step 10: Update the CheckSpec description text**

Find (current line ~1693): `"The provider that will actually run -- LLM_PROVIDER, or an active DB "` and its continuation line. Replace with wording reflecting the DB-only reality, e.g. `"The provider that will actually run -- runtime_config.provider, no env "` `"fallback"` (read the full two-line string first with `sed -n '1690,1696p' scripts/deploy.py` and adjust to fit the existing sentence structure exactly).

- [ ] **Step 11: Update tests**

```bash
grep -rln "_resolved_provider\|_resolved_provider_or_env\|_wanted_env\|_seed_slot_zero_config_if_missing\|check_provider\|check_api_key_live\|LLM_PROVIDER" tests/
```
Update every hit: drop `settings.llm_provider` fixture setup, seed `runtime_config.provider` (via `store.set_provider_override` against a test DB, or however this test suite fakes the DB layer — check the existing fixture pattern first with `grep -n "set_provider_override\|runtime_config" tests/conftest.py tests/test_deploy.py`) instead. Update call-site tests for `_wanted_env`/`_seed_slot_zero_config_if_missing` to pass `provider` explicitly.

- [ ] **Step 12: Run tests**

Run: `uv run pytest -v -k "deploy"`
Expected: PASS

- [ ] **Step 13: Commit**

```bash
cd /home/emanresu/pr-review-bot
git add scripts/deploy.py tests/
git commit -m "scripts/deploy.py: resolve provider from runtime_config only

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HjWY8hGxwRpQQqcKwZK2wp"
```

---

### Task 3: pr-review-bot — delete `doctor.py`'s local-only provider check, `_probes.llm_provider_state`, and `demo_provider_swap.py`

**Files:**
- Modify: `scripts/doctor.py` (delete `check_llm_provider`, its registration in the check list, ~line 159-178 and ~line 408)
- Modify: `scripts/_probes.py` (delete `llm_provider_state`, ~line 76-84)
- Delete: `scripts/demo_provider_swap.py`
- Test: `tests/test_probes.py` (delete `test_llm_provider_state_reports_name_and_credential_presence` and its line in the combined-probe-dict test, ~lines 58, 87-95), `tests/test_doctor.py` or equivalent (remove any `check_llm_provider`-specific test)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — this task only removes dead code once provider has no local (env-file) representation to probe.

- [ ] **Step 1: Delete `check_llm_provider` from `scripts/doctor.py`**

Remove the whole function (current lines ~159-178) and its registration line (`deploy._safe("llm-provider", check_llm_provider),`, current line ~408).

- [ ] **Step 2: Delete `llm_provider_state` from `scripts/_probes.py`**

Remove the function (current lines ~76-84).

- [ ] **Step 3: Delete `scripts/demo_provider_swap.py`**

```bash
git rm scripts/demo_provider_swap.py
```
Check for any other references first: `grep -rn "demo_provider_swap" --include="*.py" --include="*.md" --include="*.yml" --include="*.yaml" . ':!docs/superpowers/plans'` (exclude historical plan docs under `docs/superpowers/plans/`, which are a record of past work and are never edited retroactively) and remove any live (non-historical-plan) reference found, e.g. in a README "manual verification steps" list.

- [ ] **Step 4: Update `tests/test_probes.py`**

Remove `test_llm_provider_state_reports_name_and_credential_presence` and the `"provider": _probes.llm_provider_state(),` line from whatever combined-probe-dict test references it (current lines ~58, 87-95) — read the surrounding test first to edit the dict literal correctly rather than leaving a dangling comma/key.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -v -k "probes or doctor"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /home/emanresu/pr-review-bot
git add -A scripts/doctor.py scripts/_probes.py tests/
git rm scripts/demo_provider_swap.py 2>/dev/null || true
git commit -m "Delete provider env-var probe/check and demo_provider_swap.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HjWY8hGxwRpQQqcKwZK2wp"
```

---

### Task 4: pr-review-bot — full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS. Fix anything broken by Tasks 1-3 that a narrower `-k` filter didn't catch (e.g. a fixture shared across unrelated test modules that set `settings.llm_provider` for setup/teardown symmetry).

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check .`
Expected: no findings (an unused `entry`/`credential` local, or an import no longer needed after deleting a function, are the likely culprits — fix inline).

- [ ] **Step 3: grep for any remaining `llm_provider`/`LLM_PROVIDER` reference in live code**

```bash
grep -rn "llm_provider\|LLM_PROVIDER" --include="*.py" --include="*.yaml" . | grep -v "docs/superpowers/"
```
Expected: no output. (Historical `docs/superpowers/plans/`/`specs/` files are allowed to still mention it — they're a record of past decisions, never edited retroactively.)

---

### Task 5: onboarding-wizard — seed `runtime_config.provider`/`_key_index`, drop `LLM_PROVIDER` from the Render push

**Files:**
- Modify: `router.py` (`bulk_push_render_env_vars`, ~line 803-805; `_seed_slot_config`, ~line 244-304)
- Test: `tests/test_onboarding_router.py` (find the existing `_seed_slot_config` tests via `grep -n "_seed_slot_config\|slot_config" tests/test_onboarding_router.py`)

**Interfaces:**
- Consumes: nothing new — same `psycopg` raw-connection pattern `_seed_slot_config` already uses.
- Produces: `_seed_slot_config`'s return contract is unchanged (`bool`, never raises) — it now also writes `runtime_config` in the same connection/transaction.

- [ ] **Step 1: Drop the `LLM_PROVIDER` push**

In `bulk_push_render_env_vars`, delete:
```python
        env_vars["LLM_PROVIDER"] = llm_provider["provider"]
```
(current line 805) and adjust the comment at current line 797 ("Credential first, LLM_PROVIDER second: if push_env_vars fails...") to drop the now-inaccurate ordering rationale — read the surrounding 10 lines first (`sed -n '790,810p' router.py`) to edit the comment coherently rather than leaving a dangling reference.

- [ ] **Step 2: Extend `_seed_slot_config` to also seed `runtime_config`**

Add a second `conn.execute` (same connection, same `with psycopg.connect(...)` block, before or after the existing `slot_config` insert — same transaction either way since there's no explicit `conn.commit()` call visible in the current function, meaning `psycopg`'s connection context manager commits both statements together on clean exit):
```python
            conn.execute(
                f"INSERT INTO runtime_config (id, provider, {registry_key_index_column}, updated_at) "
                f"VALUES (1, %s, 0, %s) "
                f"ON CONFLICT (id) DO UPDATE SET "
                f"provider = EXCLUDED.provider, "
                f"{registry_key_index_column} = EXCLUDED.{registry_key_index_column}, "
                f"updated_at = EXCLUDED.updated_at",
                (provider, datetime.now(timezone.utc).isoformat()),
            )
```
Before writing this, check how `router.py` currently maps `provider` (e.g. `"gemini"`/`"groq"`/`"vertex"`) to its `runtime_config` key-index column name — grep this file and `providers/registry.py`-equivalent (`grep -n "KEY_INDEX_COLUMNS\|_key_index" router.py`; onboarding-wizard likely hardcodes the mapping locally rather than importing from pr-review-bot, per this project's "duplication, not import, across repos" convention documented elsewhere in CLAUDE.md — e.g. `_LLM_ENV_VAR_NAMES`). Use whatever that existing mapping already is (it must exist somewhere in `router.py`, since `_LLM_ENV_VAR_NAMES` at line 179 maps provider → credential/model env-var names already) to get `registry_key_index_column` (e.g. `"gemini_key_index"`) — build the column name string safely (an f-string built from a value looked up in a hardcoded dict keyed by `provider`, never from `provider` directly, mirroring `providers/registry.py`'s own "this dict IS the injection guard" comment for the identical concern in pr-review-bot).
Also update `_seed_slot_config`'s docstring to mention it now seeds `runtime_config.provider`/`_key_index` too, referencing `docs/superpowers/specs/2026-09-09-provider-key-index-db-only-design.md` in the sibling `pr-review-bot` repo (matching this file's existing cross-repo referencing convention).

- [ ] **Step 3: Update/extend the router test**

Find the existing test(s) asserting `_seed_slot_config` writes the right `slot_config` row and that a seed failure refuses the Render push. Extend them (or add a sibling test) to also assert the `runtime_config` row now has `provider`/`{provider}_key_index` set correctly, and that a `runtime_config` write failure (same connection, so a failure here fails the whole thing) is reported the same way the existing `slot_config`-only failure is (`{"valid": false, "reason": "slot_config_seed_failed"}` or whatever the actual reason string is — check it first with `grep -n "slot_config_seed_failed" router.py tests/test_onboarding_router.py`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest -v -k "seed_slot_config or bulk_push"`
Expected: PASS

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check .`
Expected: no findings

- [ ] **Step 6: Commit**

```bash
cd /home/emanresu/onboarding-wizard
git add router.py tests/
git commit -m "Seed runtime_config.provider/key_index; stop pushing LLM_PROVIDER

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HjWY8hGxwRpQQqcKwZK2wp"
```

---

### Task 6: onboarding-wizard — full-suite verification and CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md` (the "What sub-project 6 ... adds to these rules" section — the bullet about `slot_config` seeding and the bulk-push env-var dict should note that `provider`/`{provider}_key_index` are now seeded alongside `slot_config`, and that `LLM_PROVIDER` is no longer pushed to Render at all)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check .`
Expected: no findings

- [ ] **Step 3: Update CLAUDE.md**

Add a dated bullet (2026-09-09) under the sub-project 6 section documenting this change, in the same style as the existing dated bullets there (e.g. the "Neither VERTEX_GCP_PROJECT nor VERTEX_GCP_LOCATION is pushed..." bullet) — cross-reference `pr-review-bot`'s `docs/superpowers/specs/2026-09-09-provider-key-index-db-only-design.md`.

- [ ] **Step 4: Commit**

```bash
cd /home/emanresu/onboarding-wizard
git add CLAUDE.md
git commit -m "Document provider/key_index DB-only seeding in CLAUDE.md

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HjWY8hGxwRpQQqcKwZK2wp"
```

**Note:** per this project's CLAUDE.md, invoke the `deploy-verify` skill before any push/deploy, and run the `ui-visual-review` skill if `static/index.html` markup changed (it doesn't, in this plan) — neither applies to committing locally, only to pushing/deploying, which this plan does not do.
