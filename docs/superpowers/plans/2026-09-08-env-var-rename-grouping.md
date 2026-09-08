# Env-var rename (Vertex/Gemini/dispatcher prefix grouping) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename 5 env vars (`GCP_PROJECT`, `GCP_LOCATION`, `GCP_SERVICE_ACCOUNT_KEY`[+slots], `LLM_MODEL`, `DEFAULT_RETRY_AFTER_SECONDS`) to `VERTEX_GCP_PROJECT`, `VERTEX_GCP_LOCATION`, `VERTEX_GCP_SERVICE_ACCOUNT_KEY`[+slots], `GEMINI_MODEL`, `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS` across `pr-review-bot` and `onboarding-wizard`, so each provider/subsystem family sorts and groups together alphabetically.

**Architecture:** This is a pure rename, not new behavior — no task follows red/green TDD in the usual sense. Each task instead: runs a small Python rename script (given in full below) over an explicit file list, greps to confirm zero old-name references remain in that file list, then runs the affected test suite and confirms it's still green. `providers/registry.py`'s `PROVIDERS` dict is the single source of truth the credential/model resolution path reads from, so renaming it is what keeps DB-stored `key_index`/model overrides resolving correctly — not a separate mechanism.

**Tech Stack:** Python 3.12, pytest, ruff, ripgrep/grep.

**Spec:** `docs/superpowers/specs/2026-09-08-env-var-rename-grouping-design.md`

## Global Constraints

- Exact rename pairs (§2 of the spec): `GCP_PROJECT`→`VERTEX_GCP_PROJECT`, `GCP_LOCATION`→`VERTEX_GCP_LOCATION`, `GCP_SERVICE_ACCOUNT_KEY`(+`_1`..`_4`)→`VERTEX_GCP_SERVICE_ACCOUNT_KEY`(+`_1`..`_4`), `LLM_MODEL`→`GEMINI_MODEL`, `DEFAULT_RETRY_AFTER_SECONDS`→`DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`. Lowercase Settings-field forms rename the same way (e.g. `gcp_project`→`vertex_gcp_project`).
- **Never touch**: `.env`, `.env.config` (the real files — only their `.example` templates), `docs/superpowers/plans/**`, `docs/superpowers/specs/**` (except the plan/spec files this rename itself adds), `ISSUES.md`'s historical entries, the `runtime_config` Postgres column names, or `tests/test_config.py`'s `_RETIRED_CREDENTIAL_KEYS`/`_RETIRED_NUMBERED_RE` block (a regression guard for the *already-retired* `GCP_SERVICE_ACCOUNT_KEY_B64`/`_PATH` names from a past rename — unrelated to this one; a naive substring rename would corrupt it).
- **`cost.md` carve-out**: two clauses narrating a specific dated verification ("live and fully verified as of 2026-08-14", "Confirmed live with `LLM_MODEL=gemini-2.5-flash`") keep the old name; everything else in that file renames.
- After every task: run the affected tests. After the last task in each repo: run that repo's full suite + ruff, zero failures, zero remaining old-name references outside the excluded files above.

---

### Task 1: Rename script + core Settings/registry layer (pr-review-bot)

**Files:**
- Create: `/tmp/env_rename.py` (throwaway script, not committed — lives outside the repo)
- Modify: `config.py`, `config_deps.py`, `providers/registry.py`
- Test: `tests/test_config.py`, `tests/test_config_deps.py`, `tests/test_provider_registry.py`

**Interfaces:**
- Produces: `Settings.vertex_gcp_project`, `Settings.vertex_gcp_location`, `Settings.vertex_gcp_service_account_key`, `Settings.gemini_model`, `Settings.dispatcher_default_retry_after_seconds` — every later task's code references these exact names. `providers/registry.py::PROVIDERS["vertex"] == ("VERTEX_GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL")`, `PROVIDERS["gemini"] == ("GEMINI_API_KEY", "GEMINI_MODEL")`.

- [ ] **Step 1: Write the rename script**

```python
# /tmp/env_rename.py
import re
import sys

# (upper old, upper new) pairs. GCP_SERVICE_ACCOUNT_KEY must not touch the
# already-retired _B64/_PATH forms (tests/test_config.py's
# _RETIRED_CREDENTIAL_KEYS guards those) -- negative lookahead handles it.
PAIRS = [
    (r"GCP_PROJECT\b", "VERTEX_GCP_PROJECT"),
    (r"GCP_LOCATION\b", "VERTEX_GCP_LOCATION"),
    (r"GCP_SERVICE_ACCOUNT_KEY(?!_B64|_PATH)", "VERTEX_GCP_SERVICE_ACCOUNT_KEY"),
    (r"LLM_MODEL\b", "GEMINI_MODEL"),
    (r"DEFAULT_RETRY_AFTER_SECONDS\b", "DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS"),
]


def rename_file(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    original = text
    for upper_old, upper_new in PAIRS:
        text = re.sub(upper_old, upper_new, text)
        lower_old = upper_old.lower()
        lower_new = upper_new.lower()
        text = re.sub(lower_old, lower_new, text)
    if text != original:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"renamed: {path}")
    else:
        print(f"no change: {path}")


if __name__ == "__main__":
    for path in sys.argv[1:]:
        rename_file(path)
```

- [ ] **Step 2: Run it against this task's files**

```bash
cd ~/pr-review-bot
python3 /tmp/env_rename.py config.py config_deps.py providers/registry.py \
  tests/test_config.py tests/test_config_deps.py tests/test_provider_registry.py
```

- [ ] **Step 3: Manually fix `tests/test_config.py`'s retired-key block if the script touched it**

```bash
grep -n "_RETIRED_CREDENTIAL_KEYS\|_RETIRED_NUMBERED_RE" -A 8 tests/test_config.py
```

Confirm it still reads `"GCP_SERVICE_ACCOUNT_KEY_B64"`, `"GCP_SERVICE_ACCOUNT_KEY_PATH"` and the regex `r"^(GCP_SERVICE_ACCOUNT_KEY_B64|GCP_SERVICE_ACCOUNT_KEY_PATH)_\d+$"` unchanged (the lookahead in Step 1 should have already protected this — this step is a manual double-check, not expected to require an edit).

- [ ] **Step 4: Verify no old name remains in this task's files (outside the retired-key block)**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" config.py config_deps.py providers/registry.py tests/test_config.py tests/test_config_deps.py tests/test_provider_registry.py
grep -n "GCP_SERVICE_ACCOUNT_KEY" config.py config_deps.py providers/registry.py tests/test_config.py tests/test_config_deps.py tests/test_provider_registry.py | grep -v "_B64\|_PATH"
```

Expected: no output from either command.

- [ ] **Step 5: Run the affected tests**

```bash
uv run pytest tests/test_config.py tests/test_config_deps.py tests/test_provider_registry.py -v
```

Expected: PASS, same count as before the rename.

- [ ] **Step 6: Commit**

```bash
git add config.py config_deps.py providers/registry.py tests/test_config.py tests/test_config_deps.py tests/test_provider_registry.py
git commit -m "Rename GCP_*/LLM_MODEL/DEFAULT_RETRY_AFTER_SECONDS in Settings + provider registry

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 2: Provider resolution layer (pr-review-bot)

**Files:**
- Modify: `providers/vertex_credentials.py`, `providers/active_model.py`, `providers/base.py`, `providers/google_genai.py`, `providers/groq.py`, `providers/factory.py`
- Test: `tests/test_vertex_credentials.py`, `tests/test_active_model.py`, `tests/test_providers.py`

**Interfaces:**
- Consumes: `Settings.vertex_gcp_project`, `Settings.vertex_gcp_location`, `Settings.vertex_gcp_service_account_key`, `Settings.gemini_model`, `Settings.dispatcher_default_retry_after_seconds` (Task 1), `providers.registry.PROVIDERS` (Task 1).

- [ ] **Step 1: Run the rename script**

```bash
cd ~/pr-review-bot
python3 /tmp/env_rename.py providers/vertex_credentials.py providers/active_model.py \
  providers/base.py providers/google_genai.py providers/groq.py providers/factory.py \
  tests/test_vertex_credentials.py tests/test_active_model.py tests/test_providers.py
```

- [ ] **Step 2: Verify no old name remains**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS|gcp_project|gcp_location|llm_model|default_retry_after_seconds)\b" providers/vertex_credentials.py providers/active_model.py providers/base.py providers/google_genai.py providers/groq.py providers/factory.py tests/test_vertex_credentials.py tests/test_active_model.py tests/test_providers.py
grep -n "GCP_SERVICE_ACCOUNT_KEY\|gcp_service_account_key" providers/vertex_credentials.py providers/active_model.py providers/base.py providers/google_genai.py providers/groq.py providers/factory.py tests/test_vertex_credentials.py tests/test_active_model.py tests/test_providers.py | grep -v "_B64\|_PATH"
```

Expected: no output.

- [ ] **Step 3: Run the affected tests**

```bash
uv run pytest tests/test_vertex_credentials.py tests/test_active_model.py tests/test_providers.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add providers/vertex_credentials.py providers/active_model.py providers/base.py providers/google_genai.py providers/groq.py providers/factory.py tests/test_vertex_credentials.py tests/test_active_model.py tests/test_providers.py
git commit -m "Rename env vars in provider resolution layer

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 3: render_client.py + scripts (pr-review-bot)

**Files:**
- Modify: `render_client.py`, `scripts/deploy.py`, `scripts/encode_credential.py`, `scripts/set_override.py`, `scripts/init_env.py`, `scripts/manual_verify_vertex.py`
- Test: `tests/test_render_client.py`, `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `providers.registry.PROVIDERS` (Task 1), `Settings.vertex_gcp_project`/`vertex_gcp_location`/`vertex_gcp_service_account_key`/`gemini_model`/`dispatcher_default_retry_after_seconds` (Task 1).

- [ ] **Step 1: Run the rename script**

```bash
cd ~/pr-review-bot
python3 /tmp/env_rename.py render_client.py scripts/deploy.py scripts/encode_credential.py \
  scripts/set_override.py scripts/init_env.py scripts/manual_verify_vertex.py \
  tests/test_render_client.py tests/test_deploy_script.py
```

- [ ] **Step 2: Verify no old name remains**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS|gcp_project|gcp_location|llm_model|default_retry_after_seconds)\b" render_client.py scripts/deploy.py scripts/encode_credential.py scripts/set_override.py scripts/init_env.py scripts/manual_verify_vertex.py tests/test_render_client.py tests/test_deploy_script.py
grep -n "GCP_SERVICE_ACCOUNT_KEY\|gcp_service_account_key" render_client.py scripts/deploy.py scripts/encode_credential.py scripts/set_override.py scripts/init_env.py scripts/manual_verify_vertex.py tests/test_render_client.py tests/test_deploy_script.py | grep -v "_B64\|_PATH"
```

Expected: no output.

- [ ] **Step 3: Run the affected tests**

```bash
uv run pytest tests/test_render_client.py tests/test_deploy_script.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add render_client.py scripts/deploy.py scripts/encode_credential.py scripts/set_override.py scripts/init_env.py scripts/manual_verify_vertex.py tests/test_render_client.py tests/test_deploy_script.py
git commit -m "Rename env vars in render_client + operator scripts

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 4: Dashboard (pr-review-bot)

**Files:**
- Modify: `dashboard/environment.py`, `dashboard/static/dashboard.html`
- Test: `dashboard/tests/test_dashboard_page.py`, `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `providers.registry.PROVIDERS`, `Settings` fields (Task 1); `dashboard/static/dashboard.html`'s `ENV_VAR_DESCRIPTIONS` dict keys, `CONFIG_FIELD_ENV_VAR` mapping, and `CREDENTIAL_SLOT_BASE_VARS` array rename together (client-side JS, same file).

- [ ] **Step 1: Run the rename script**

```bash
cd ~/pr-review-bot
python3 /tmp/env_rename.py dashboard/environment.py dashboard/static/dashboard.html \
  dashboard/tests/test_dashboard_page.py dashboard/tests/test_environment.py
```

- [ ] **Step 2: Verify no old name remains**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" dashboard/environment.py dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py dashboard/tests/test_environment.py
grep -n "GCP_SERVICE_ACCOUNT_KEY" dashboard/environment.py dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py dashboard/tests/test_environment.py | grep -v "_B64\|_PATH"
```

Expected: no output.

- [ ] **Step 3: Syntax-check dashboard.html's embedded JS still parses**

```bash
python3 - <<'EOF'
import re
html = open("dashboard/static/dashboard.html", encoding="utf-8").read()
m = re.search(r"<script>(.*)</script>", html, re.S)
open("/tmp/dash_script_check.js", "w", encoding="utf-8").write(m.group(1))
EOF
node --check /tmp/dash_script_check.js && echo SYNTAX_OK
```

(If no local `node`, try `"/mnt/c/Program Files/nodejs/node.exe" --check /tmp/dash_script_check.js`.)

- [ ] **Step 4: Run the affected tests**

```bash
uv run pytest dashboard/tests/test_dashboard_page.py dashboard/tests/test_environment.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/environment.py dashboard/static/dashboard.html dashboard/tests/test_dashboard_page.py dashboard/tests/test_environment.py
git commit -m "Rename env vars in dashboard Environment tab

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 5: Config templates + render.yaml (pr-review-bot)

**Files:**
- Modify: `render.yaml`, `.env.example`, `.env.config.example`

**Interfaces:** none (leaf task, no other task reads these).

- [ ] **Step 1: Run the rename script**

```bash
cd ~/pr-review-bot
python3 /tmp/env_rename.py render.yaml .env.example .env.config.example
```

- [ ] **Step 2: Verify no old name remains**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" render.yaml .env.example .env.config.example
grep -n "GCP_SERVICE_ACCOUNT_KEY" render.yaml .env.example .env.config.example | grep -v "_B64\|_PATH"
```

Expected: no output.

- [ ] **Step 3: Manually confirm render.yaml's YAML is still well-formed**

```bash
python3 -c "import yaml; yaml.safe_load(open('render.yaml'))" && echo YAML_OK
```

- [ ] **Step 4: Commit**

```bash
git add render.yaml .env.example .env.config.example
git commit -m "Rename env vars in render.yaml + .env templates

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 6: Current-state docs (pr-review-bot)

**Files:**
- Modify: `CLAUDE.md`, `SPEC.md`, `cost.md`, `guide/reference/sync-env.md`, `guide/reference/config.md`, `guide/setup/04-llm-provider.md`, `guide/operations/overrides.md`, `guide/background/providers.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Run the rename script on every file except cost.md**

```bash
cd ~/pr-review-bot
python3 /tmp/env_rename.py CLAUDE.md SPEC.md guide/reference/sync-env.md guide/reference/config.md \
  guide/setup/04-llm-provider.md guide/operations/overrides.md guide/background/providers.md
```

- [ ] **Step 2: Rename cost.md by hand, preserving the two dated clauses**

Open `cost.md` and confirm these two clauses (currently around its §4 "Free-tier headroom" section) still read exactly:
- `live and fully verified as of 2026-08-14 (\`LLM_PROVIDER=vertex\`)` — unchanged (`LLM_PROVIDER` isn't part of this rename anyway).
- `the shared \`LLM_MODEL\` default not existing as a Vertex publisher model` — **keep as `LLM_MODEL`**, this narrates what was actually set at that time.
- `Confirmed live with \`LLM_MODEL=gemini-2.5-flash\`` — **keep as `LLM_MODEL`**.

Then run the rename script on the rest of the file and manually revert just those two `LLM_MODEL` occurrences back:

```bash
python3 /tmp/env_rename.py cost.md
grep -n "GEMINI_MODEL" cost.md
```

For each match that falls inside the two dated clauses above, change `GEMINI_MODEL` back to `LLM_MODEL` by hand (use the Edit tool, not sed, since this is exactly two specific occurrences).

- [ ] **Step 3: Verify no old name remains outside the two preserved clauses**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" CLAUDE.md SPEC.md guide/reference/sync-env.md guide/reference/config.md guide/setup/04-llm-provider.md guide/operations/overrides.md guide/background/providers.md
grep -n "GCP_SERVICE_ACCOUNT_KEY" CLAUDE.md SPEC.md guide/reference/sync-env.md guide/reference/config.md guide/setup/04-llm-provider.md guide/operations/overrides.md guide/background/providers.md | grep -v "_B64\|_PATH"
grep -n "LLM_MODEL" cost.md
```

Expected: first two commands print nothing; the `cost.md` grep prints exactly the two preserved clauses from Step 2 and nothing else.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md SPEC.md cost.md guide/reference/sync-env.md guide/reference/config.md guide/setup/04-llm-provider.md guide/operations/overrides.md guide/background/providers.md
git commit -m "Rename env vars in current-state docs (CLAUDE.md, SPEC.md, guide/)

cost.md keeps two clauses narrating a specific dated verification under
the old LLM_MODEL name, per the design spec's carve-out.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 7: Final verification sweep (pr-review-bot)

**Files:** none modified; verification only.

- [ ] **Step 1: Repo-wide grep for any surviving old name outside excluded paths**

```bash
cd ~/pr-review-bot
grep -rnE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" \
  --include=*.py --include=*.md --include=*.html --include=*.yaml --include=*.example . \
  | grep -v "docs/superpowers/plans\|docs/superpowers/specs\|ISSUES.md\|cost.md"
grep -rn "GCP_SERVICE_ACCOUNT_KEY" --include=*.py --include=*.md --include=*.html --include=*.yaml --include=*.example . \
  | grep -v "docs/superpowers/plans\|docs/superpowers/specs\|ISSUES.md" \
  | grep -v "_B64\|_PATH"
grep -n "LLM_MODEL" cost.md
```

Expected: the first two commands print nothing; the third prints exactly the two preserved `cost.md` clauses.

- [ ] **Step 2: Full test suite + ruff**

```bash
uv run ruff check .
uv run pytest -q
```

Expected: ruff clean, full suite green (same test count as before this plan started).

- [ ] **Step 3: deploy-verify skill**

Invoke the `deploy-verify` skill (build the Docker image, boot-smoke-test `import main`) — confirms the rename didn't break anything the dev venv's own test run can't catch.

---

### Task 8: onboarding-wizard — router.py + tests

**Files:**
- Modify: `~/onboarding-wizard/router.py`
- Test: `~/onboarding-wizard/tests/test_onboarding_router.py`

**Interfaces:**
- Produces: `_LLM_ENV_VAR_NAMES["vertex"] == ("VERTEX_GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL")`, `_LLM_ENV_VAR_NAMES["gemini"] == ("GEMINI_API_KEY", "GEMINI_MODEL")`, `_GENERIC_OPERATIONAL_ENV_DEFAULTS` keyed by `VERTEX_GCP_LOCATION` (still excluding `VERTEX_GCP_PROJECT`, same as it excluded `GCP_PROJECT` today) and `DISPATCHER_DEFAULT_RETRY_AFTER_SECONDS`.

- [ ] **Step 1: Copy the rename script and run it**

```bash
cp /tmp/env_rename.py ~/onboarding-wizard/env_rename_tmp.py
cd ~/onboarding-wizard
python3 env_rename_tmp.py router.py tests/test_onboarding_router.py
rm env_rename_tmp.py
```

- [ ] **Step 2: Manually confirm the GCP_PROJECT exclusion comment survived**

```bash
grep -n "VERTEX_GCP_PROJECT" -B 3 -A 3 router.py
```

Confirm the comment explaining why `VERTEX_GCP_PROJECT` (was `GCP_PROJECT`) is deliberately excluded from `_GENERIC_OPERATIONAL_ENV_DEFAULTS` still reads sensibly — fix the wording by hand if the rename script left an awkward old-name reference inside the prose comment (the script only renames the token itself, not surrounding English prose that might reference "GCP_PROJECT" by name in a sentence).

- [ ] **Step 3: Verify no old name remains**

```bash
grep -nE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" router.py tests/test_onboarding_router.py
grep -n "GCP_SERVICE_ACCOUNT_KEY" router.py tests/test_onboarding_router.py | grep -v "_B64\|_PATH"
```

Expected: no output.

- [ ] **Step 4: Run the affected tests**

```bash
uv run pytest tests/test_onboarding_router.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_onboarding_router.py
git commit -m "Rename Vertex/Gemini/dispatcher env vars to match pr-review-bot's rename

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

---

### Task 9: onboarding-wizard — CLAUDE.md + final sweep

**Files:**
- Modify: `~/onboarding-wizard/CLAUDE.md`

- [ ] **Step 1: Run the rename script**

```bash
cp /tmp/env_rename.py ~/onboarding-wizard/env_rename_tmp.py
cd ~/onboarding-wizard
python3 env_rename_tmp.py CLAUDE.md
rm env_rename_tmp.py
```

- [ ] **Step 2: Repo-wide grep for any surviving old name**

```bash
grep -rnE "\b(GCP_PROJECT|GCP_LOCATION|LLM_MODEL|DEFAULT_RETRY_AFTER_SECONDS)\b" \
  --include=*.py --include=*.md --include=*.js --include=*.html . \
  | grep -v "docs/superpowers/plans\|docs/superpowers/specs"
grep -rn "GCP_SERVICE_ACCOUNT_KEY" --include=*.py --include=*.md --include=*.js --include=*.html . \
  | grep -v "docs/superpowers/plans\|docs/superpowers/specs" | grep -v "_B64\|_PATH"
```

Expected: no output.

- [ ] **Step 3: Full test suite**

```bash
uv run pytest -q  # or this repo's equivalent test-run command if different (check its README/CLAUDE.md)
```

Expected: green, same test count as before this plan started.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Rename env vars in onboarding-wizard CLAUDE.md

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01E7EZmNTVxkednGtMk8qN3f"
```

## Self-Review Notes

- **Spec coverage:** §2 (5 renames) → Tasks 1-4, 8. §3 exclusions → enforced by every task's verify step + Task 1's negative-lookahead script + Task 6/9's manual carve-outs. §4 cascade mechanics → Tasks 1-4 follow that exact layering (Settings/registry first, then everything downstream). §5 verification → Task 2 exercises the resolution path directly via its test files; Task 7 covers the full-suite check. §6 file list → Tasks 1-6, 8-9 cover every listed file. §7 execution shape → two independent repos, own commits, own test runs, matches Tasks 1-7 vs 8-9 split.
- **Placeholder scan:** every step has a runnable command or a fully-written script; no "add appropriate renames" language anywhere.
- **Type consistency:** `Settings` field names introduced in Task 1 (`vertex_gcp_project`, `vertex_gcp_location`, `vertex_gcp_service_account_key`, `gemini_model`, `dispatcher_default_retry_after_seconds`) are the exact names every later task's "Consumes" line references.
