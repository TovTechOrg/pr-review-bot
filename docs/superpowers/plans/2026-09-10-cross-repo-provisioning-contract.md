# Cross-Repo Provisioning Contract (Bot Side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the env-var placement partition and the `runtime_config`/`slot_config` provisioning split that `pr-review-bot` already maintains internally as a generated, committed, CI-enforced data file (`contracts/provisioning.json`), so a sibling provisioner can be checked against it without either repo importing the other's code.

**Architecture:** A new `scripts/gen_contract.py` mirrors `scripts/gen_docs.py` exactly — it reads module constants and `Settings` **class** metadata (never the `settings` instance), renders a deterministic JSON document, and writes it to one fixed path. CI's existing `docs` job regenerates it and byte-compares, the same `git add` / `git diff --cached --exit-code` gate that already keeps `guide/reference/` honest. A new `tests/test_provisioning_contract.py` holds the blocking bot-side assertions: the committed file matches the generator, the contract's `provisioner_required` is exactly the set the bot cannot derive for itself, every other declared column is `ADD COLUMN`-able, and no `db_only` key has a `render.yaml` entry.

**Tech Stack:** Python 3.12, pydantic / pydantic-settings, `json` (stdlib), PyYAML (tests only), pytest (+ pytest-xdist, `-n 4`), ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md` — this plan implements **Stage 2 only** (that spec's §10 rollout step 2: `scripts/gen_contract.py`, `contracts/provisioning.json`, the `docs`-job freshness step, and §6.1's bot-side tests). Read §5 and §6.1 before starting; every task below argues from them.

## Global Constraints

- **Stage 1 is already landed and merged to `main`.** `review_queue/runtime_config_defaults.py` (`COLUMN_TO_SETTING`, `declared_defaults()`, `NO_DEFAULT_BY_DESIGN`), `store._widen_statements()` / `store._backfill_runtime_config()`, `cooldown_config.problems()` / `usage_cap_config.problems()`, and `main.py`'s three-group boot gate all exist. This plan **reads** them and changes none of them. If a task seems to require editing one, stop and report rather than editing.
- **THE ONE RULE, verbatim from `scripts/gen_docs.py` and `review_queue/runtime_config_defaults.py`: read the `Settings` **class**'s `model_fields[...].default`, never the module-level `settings` instance.** The generated file is committed here and vendored into another repository. `config.settings` carries this machine's real `DATABASE_URL`, API keys, and service-account material; `Settings.model_fields` carries declared defaults only. `scripts/gen_contract.py` must not import `settings` at all — not directly, and not by reaching through `deploy.settings`. See `CLAUDE.md`'s "Secret handling" section, which overrides everything here.
- **The contract carries env-var *names* and non-secret operational defaults only, never a credential value** (spec §5.3). `env_vars` entries deliberately carry a placement and nothing else — no value, no default — so there is no shape in which a credential's default could appear. `runtime_config.bot_backfilled` is the only block that carries values, and every one of them comes from `runtime_config_defaults.declared_defaults()`, whose inputs are all `OPERATIONAL_KEYS` members.
- **Output must be DETERMINISTIC** — no timestamps, no unordered iteration, no absolute paths (spec §5.4). CI compares byte-for-byte, so any run-to-run variation is a permanently red build rather than a useful signal. Every set-derived list is `sorted()`; every schema-derived list follows the declared tuple's own order.
- **Explicit `encoding="utf-8"` and `newline="\n"` on every write.** `.gitattributes` pins the working tree to LF; a locale-default write would fail the byte-compare on a Windows operator's machine and nowhere else. `tests/test_gen_docs.py::test_every_file_call_in_gen_docs_declares_encoding_and_newline` is the existing precedent, and this plan adds the same guard for the new generator.
- **Out of scope, do not touch:** anything in the `onboarding-wizard` repository (spec §7, §6.1's wizard-side tests, `update_bot_contract.py`) — that is a separate repo, out of reach here; spec §6.2's advisory cross-repo job (Stage 4); spec §9's `CLAUDE.md` rollout note (Stage 5); any change to `dashboard/static/`.
- **Before pushing:** `uv run pytest -v` and `uv run ruff check .` must both be clean, and (per `CLAUDE.md`) any push to `main` needs the `deploy-verify` skill. This plan ends with commits on a branch — **never `git push`**.
- Fast iteration while working: `uv run pytest -m "not db" -n 4`. Full suite before each commit.
- ruff `line-length = 100`.

---

### Task 1: The contract's content — `scripts/gen_contract.py`

The four blocks of spec §5.2, each derived from a constant that already exists. No file writing yet, no committed artifact yet: this task's deliverable is a pure `build_contract() -> dict` plus the secret-discipline guards, so a reviewer can gate the *content* decisions before any bytes land on disk.

Four derivations, each pinned by a test below:

1. **`env_vars`** — every name a provisioner might have to push, mapped to exactly one of five placements. The five placements are the five sync destinations `tests/test_deploy_script.py::test_operational_keys_partition_cleanly_across_every_sync_destination` (line 1583) already asserts partition `OPERATIONAL_KEYS` cleanly. The name set is `OPERATIONAL_KEYS | _ALWAYS_SYNCED`, not `OPERATIONAL_KEYS` alone: spec §5.3 says "No `always_synced` entry (**all credentials and identity**) carries a default at all", which only makes sense if the always-synced credential/identity vars are present as names.
2. **`providers`** — `registry.PROVIDERS` joined to `registry.KEY_INDEX_COLUMNS`.
3. **`runtime_config`** — `store.RUNTIME_CONFIG_COLUMNS` partitioned into the columns the bot fills (`runtime_config_defaults.declared_defaults()`), the one it deliberately leaves NULL (`NO_DEFAULT_BY_DESIGN`), the three slot-index columns (exactly one of which a provisioner writes), and — by subtraction — the three the provisioner must supply.
4. **`slot_config`** — `store.SLOT_CONFIG_COLUMNS` split by one small declared constant naming the two Vertex-only optional columns.

**Files:**
- Create: `scripts/gen_contract.py`
- Test: `tests/test_provisioning_contract.py`
- Read (do not modify): `config.py:18-45` (`OPERATIONAL_KEYS`), `scripts/deploy.py:62-79` (`_ALWAYS_SYNCED`), `scripts/deploy.py:113` (`_GENERIC_OPERATIONAL_ENV_ATTRS`), `scripts/deploy.py:125-146` (`_DB_SYNCED_OPERATIONAL_KEYS`), `scripts/deploy.py:152` (`_NEVER_SYNCED_OPERATIONAL_KEYS`), `providers/registry.py:14-40`, `review_queue/store.py:44-82`, `review_queue/runtime_config_defaults.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (Stage 1 is already on `main`).
- Produces:
  - `scripts.gen_contract.CONTRACT_PATH: str` — `"contracts/provisioning.json"`, the one path the generator may write.
  - `scripts.gen_contract.CONTRACT_VERSION: int` — `1`.
  - `scripts.gen_contract.GENERATED_BY: str` — `"scripts.gen_contract -- do not edit by hand"`.
  - `scripts.gen_contract.env_vars() -> dict[str, dict[str, str]]`
  - `scripts.gen_contract.providers() -> dict[str, dict[str, str]]`
  - `scripts.gen_contract.runtime_config() -> dict[str, object]`
  - `scripts.gen_contract.slot_config() -> dict[str, list[str]]`
  - `scripts.gen_contract.build_contract() -> dict[str, object]`
  - Task 2 adds `render()`, `write_contract()` and `main()` to the same module.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_provisioning_contract.py
"""The provisioning contract: what the bot publishes for a provisioner.

Two independent things are pinned here. First, the CONTENT is derived --
every entry comes from a module constant or from Settings CLASS metadata,
never from a hand-typed list and never from the live `settings` instance,
which carries this machine's real credentials into a file that is
committed here and vendored into another repository. Second, the contract
cannot LIE about ownership: what it calls provisioner_required must be
exactly what the bot genuinely cannot fill in for itself.

See docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md
sections 5 and 6.1.
"""
from __future__ import annotations

import pytest

from config import OPERATIONAL_KEYS, settings
from providers import registry
from review_queue import runtime_config_defaults, store
from scripts import deploy, gen_contract

SENTINEL = "SENTINEL-3f0c71ba9d42e6c8-MUST-NOT-BE-VENDORED"


def test_env_vars_covers_every_operational_key_and_every_always_synced_var():
    """The two name sets a provisioner has to get right. OPERATIONAL_KEYS is
    the placement surface proper; _ALWAYS_SYNCED adds the credential and
    identity vars that are pushed to Render on every sync -- names only,
    per the spec's section 5.3."""
    expected = set(OPERATIONAL_KEYS) | set(deploy._ALWAYS_SYNCED)
    assert set(gen_contract.env_vars()) == expected


def test_every_env_var_entry_carries_exactly_one_placement_and_nothing_else():
    """Names and placements only. An entry that could carry a value is the
    shape in which a credential's default would eventually be published."""
    for name, entry in gen_contract.env_vars().items():
        assert set(entry) == {"placement"}, f"{name} carries more than a placement"
        assert isinstance(entry["placement"], str)


def test_placements_match_the_sync_destinations_deploy_actually_uses():
    entries = gen_contract.env_vars()
    assert entries["GITHUB_TARGET_REPO"]["placement"] == "always_synced"
    assert entries["KEY_USAGE_TOKEN_CAP"]["placement"] == "db_only"
    assert entries["RENDER_SERVICE_NAME"]["placement"] == "never_synced"
    assert entries["GEMINI_MODEL"]["placement"] == "slot_zero_seed"
    db_only = {n for n, e in entries.items() if e["placement"] == "db_only"}
    assert db_only == set(deploy._DB_SYNCED_OPERATIONAL_KEYS)
    never = {n for n, e in entries.items() if e["placement"] == "never_synced"}
    assert never == set(deploy._NEVER_SYNCED_OPERATIONAL_KEYS)
    seeded = {n for n, e in entries.items() if e["placement"] == "slot_zero_seed"}
    assert seeded == {model_var for _cred, model_var in registry.PROVIDERS.values()}


def test_a_name_in_no_sync_destination_is_a_loud_failure():
    """The generator must never emit a contract for a partition it cannot
    resolve -- a silently-omitted or silently-mislabelled key is exactly the
    rename hazard this contract exists to catch (spec section 5.1)."""
    with pytest.raises(ValueError, match="sync destination"):
        gen_contract._placement("NOT_A_REAL_ENV_VAR")


def test_providers_block_names_each_providers_credential_model_and_slot_column():
    block = gen_contract.providers()
    assert set(block) == set(registry.PROVIDERS)
    for provider, (credential_var, model_var) in registry.PROVIDERS.items():
        assert block[provider] == {
            "credential_var": credential_var,
            "model_var": model_var,
            "key_index_column": registry.KEY_INDEX_COLUMNS[provider],
        }


def test_runtime_config_block_partitions_every_declared_column_exactly_once():
    """The four lists must cover RUNTIME_CONFIG_COLUMNS with no gaps and no
    overlaps -- a column in none of them is one nobody has been told to
    write, and a column in two is a contradiction about who owns it."""
    block = gen_contract.runtime_config()
    groups = {
        "provisioner_required": set(block["provisioner_required"]),
        "provisioner_required_one_of": set(block["provisioner_required_one_of"]),
        "bot_backfilled": {entry["column"] for entry in block["bot_backfilled"]},
        "no_default_by_design": set(block["no_default_by_design"]),
    }
    declared = {name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS}
    union = set().union(*groups.values())
    assert union == declared, f"symmetric difference: {sorted(union ^ declared)}"
    seen: set[str] = set()
    for label, group in groups.items():
        overlap = seen & group
        assert not overlap, f"{label} overlaps an earlier group on: {sorted(overlap)}"
        seen |= group


def test_runtime_config_block_matches_the_stage_one_constants():
    block = gen_contract.runtime_config()
    assert block["provisioner_required"] == ["id", "provider", "updated_at"]
    assert block["provisioner_required_one_of"] == sorted(registry.KEY_INDEX_COLUMNS.values())
    assert block["no_default_by_design"] == sorted(runtime_config_defaults.NO_DEFAULT_BY_DESIGN)
    defaults = runtime_config_defaults.declared_defaults()
    assert {e["column"]: e["default"] for e in block["bot_backfilled"]} == defaults


def test_bot_backfilled_entries_carry_the_declared_sql_type():
    by_name = dict(store.RUNTIME_CONFIG_COLUMNS)
    for entry in gen_contract.runtime_config()["bot_backfilled"]:
        assert entry["sql_type"] == by_name[entry["column"]]
    entries = {e["column"]: e for e in gen_contract.runtime_config()["bot_backfilled"]}
    assert entries["cooldown_base_seconds"]["sql_type"] == "DOUBLE PRECISION"
    assert entries["cooldown_base_seconds"]["default"] == 300.0


def test_the_reset_time_default_is_serialized_as_its_three_part_wire_form():
    """key_usage_reset_time_utc is a `time` on Settings and TEXT in the
    column. The contract must carry the wire form usage_cap_config parses
    back with time.fromisoformat -- that single conversion, done once here,
    is what replaces a hand-typed "04:00:00" literal in the other repo
    (spec section 5.2)."""
    entries = {e["column"]: e for e in gen_contract.runtime_config()["bot_backfilled"]}
    assert entries["key_usage_reset_time_utc"]["default"] == "04:00:00"
    assert entries["key_usage_reset_time_utc"]["sql_type"] == "TEXT"


def test_slot_config_block_splits_required_from_the_vertex_only_optionals():
    block = gen_contract.slot_config()
    assert block["provisioner_required"] == ["provider", "slot_index", "model", "updated_at"]
    assert block["optional"] == ["vertex_gcp_project", "vertex_gcp_location"]
    declared = [name for name, _sql_type in store.SLOT_CONFIG_COLUMNS]
    assert sorted(block["provisioner_required"] + block["optional"]) == sorted(declared)


def test_model_is_provisioner_required_even_though_the_column_is_nullable():
    """`model` is nullable in the DDL, but providers/active_model.py's own
    docstring calls a slot with no configured model "a real missing-config
    state" that factory._build turns into a visible failure. Nullability is
    a widening constraint, not a statement about who owns the value."""
    assert "model" in gen_contract.slot_config()["provisioner_required"]


def test_build_contract_carries_the_marker_version_and_all_four_blocks():
    contract = gen_contract.build_contract()
    assert contract["generated_by"] == gen_contract.GENERATED_BY
    assert "do not edit" in contract["generated_by"].lower()
    assert "scripts.gen_contract" in contract["generated_by"]
    assert contract["contract_version"] == gen_contract.CONTRACT_VERSION
    assert set(contract) == {
        "generated_by", "contract_version",
        "env_vars", "providers", "runtime_config", "slot_config",
    }


def test_build_contract_is_deterministic():
    """CI compares byte-for-byte, so any run-to-run variation is a
    permanently red build rather than a useful signal."""
    assert gen_contract.build_contract() == gen_contract.build_contract()


def test_the_contract_never_contains_a_configured_value(monkeypatch):
    """The behavioural half of THE ONE RULE. If any derivation is ever
    changed to read `settings` (or `deploy.settings`) instead of the
    Settings class, this fails -- and this file is vendored into another
    repository, so a leak here does not stay local."""
    for field in ("database_url", "github_webhook_secret", "groq_api_key",
                  "gemini_api_key", "vertex_gcp_service_account_key",
                  "github_app_private_key", "render_api_key",
                  "dashboard_password", "dashboard_session_secret"):
        monkeypatch.setattr(settings, field, SENTINEL, raising=False)
    assert SENTINEL not in repr(gen_contract.build_contract())


def test_gen_contract_module_does_not_import_the_settings_instance():
    """Static guard complementing the behavioural one above: importing the
    singleton at all is the mistake. Parsed with ast rather than grepped --
    a source grep would also match the module's own docstring explaining the
    rule, a false positive that has already bitten this project once
    (see tests/test_gen_docs.py's equivalent)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gen_contract))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            assert "settings" not in imported, (
                f"line {node.lineno} imports the settings instance from {node.module}"
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: collection error — `ImportError: cannot import name 'gen_contract' from 'scripts'`.

- [ ] **Step 3: Write the generator**

```python
# scripts/gen_contract.py
"""Generate contracts/provisioning.json -- what a provisioner must supply.

    uv run python -m scripts.gen_contract

This publishes a partition this repository already maintains internally: which
env vars go to Render and which are database-only, which provider maps to which
credential/model/slot-index name, and which runtime_config / slot_config values
the provisioner must write versus which this service fills in for itself at
boot (review_queue/store.py's widen + backfill). Nothing here is new policy --
it is the existing policy, written down where another repository can read it
without importing any of this one's code.

THE ONE RULE, the same one scripts/gen_docs.py and
review_queue/runtime_config_defaults.py state: every derivation reads MODULE
CONSTANTS and the Settings CLASS's model_fields[...].default, and NEVER
config.settings. model_fields carries each field's DECLARED default; the
settings instance carries this machine's real DATABASE_URL, API keys, and
service-account material. This file is committed here AND vendored verbatim
into the onboarding-wizard repository, so reading the instance would not just
publish those values, it would copy them into a second repository. Import the
Settings CLASS only (transitively, via runtime_config_defaults); the
module-level instance must never be imported here.
tests/test_provisioning_contract.py pins this both behaviourally and by
parsing this module's own import statements.

The contract carries env-var NAMES and non-secret operational DEFAULTS only.
env_vars() entries deliberately carry a placement and nothing else, so there is
no shape in which a credential's default could appear; runtime_config's
bot_backfilled block is the only one carrying values, and every one of them
comes from an OPERATIONAL_KEYS member.

Output must be DETERMINISTIC -- no timestamps, no unordered iteration, no
absolute paths. CI's docs job compares byte-for-byte, so any run-to-run
variation is a permanently red build rather than a useful signal. Every
set-derived list below is sorted(); every schema-derived list follows its
declared tuple's own order.
"""

from __future__ import annotations

from config import OPERATIONAL_KEYS
from providers import registry
from review_queue import runtime_config_defaults, store
from scripts import deploy

# The only file this generator may write. Fixed, not configurable, for the
# same reason gen_docs.py fixes REFERENCE_DIR: a generator that can be
# pointed anywhere is one wrong argument away from replacing hand-written
# content.
CONTRACT_PATH = "contracts/provisioning.json"

# Bumped only when the SHAPE changes (a new block, a renamed key, a changed
# entry shape) -- never when an entry's content changes, which happens on
# any ordinary schema or env-var edit and is what the byte-compare already
# catches. The consumer reads this to know whether it understands the file
# at all.
CONTRACT_VERSION = 1

GENERATED_BY = "scripts.gen_contract -- do not edit by hand"

# The five sync destinations scripts/deploy.py partitions OPERATIONAL_KEYS
# across, in the same order tests/test_deploy_script.py's
# test_operational_keys_partition_cleanly_across_every_sync_destination
# checks them. Derived from deploy's own constants, never re-typed: a
# key moved from one group to another there changes this file's output on
# the next run, which is the whole point of the freshness gate.
#
# slot_zero_seed is the one group with no constant of its own in deploy.py
# -- the three model vars reach the database exactly once, via
# _seed_slot_zero_config_if_missing(), rather than being pushed on every
# sync like every other group. Derived from registry.PROVIDERS so a fourth
# provider is picked up automatically.
_SLOT_ZERO_SEEDED = frozenset(model_var for _credential, model_var in registry.PROVIDERS.values())

_PLACEMENTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("always_synced", frozenset(deploy._ALWAYS_SYNCED)),
    ("generic_operational", frozenset(deploy._GENERIC_OPERATIONAL_ENV_ATTRS)),
    ("slot_zero_seed", _SLOT_ZERO_SEEDED),
    ("db_only", frozenset(deploy._DB_SYNCED_OPERATIONAL_KEYS)),
    ("never_synced", frozenset(deploy._NEVER_SYNCED_OPERATIONAL_KEYS)),
)

# slot_config columns a provisioner may legitimately leave NULL: the two
# Vertex-only overrides. Both are meaningless for gemini/groq (deploy.py's
# _seed_slot_zero_config_if_missing writes NULL for them unless the provider
# is vertex), and vertex_gcp_project is optional even for vertex ("unset
# means use the project_id embedded in the resolved service-account key" --
# config.py's own field comment).
#
# `model` is deliberately NOT here even though its column is nullable.
# Nullability in SLOT_CONFIG_COLUMNS is a widening constraint (ADD COLUMN
# NOT NULL fails against a populated table -- see
# store._widening_safe_sql_type), not a statement about ownership:
# providers/active_model.py calls a slot with no configured model "a real
# missing-config state" that factory._build turns into a visible failure.
_SLOT_CONFIG_OPTIONAL: tuple[str, ...] = ("vertex_gcp_project", "vertex_gcp_location")


def _placement(name: str) -> str:
    """Which of the five sync destinations `name` belongs to.

    Raises rather than guessing. A key in no destination is one nobody
    syncs anywhere -- ISSUES.md's 2026-08-17 "--sync-env silently never
    pushes 12 of the documented operational env vars" entry is what that
    looks like in production -- and a key in two is a contradiction. Either
    way, emitting a contract for it would publish a claim this repository
    cannot honour.
    """
    found = [label for label, members in _PLACEMENTS if name in members]
    if len(found) != 1:
        raise ValueError(
            f"{name} lands in {len(found)} sync destination(s) "
            f"({', '.join(found) or 'none'}) -- exactly one is required. Fix the "
            "partition in scripts/deploy.py before regenerating the contract."
        )
    return found[0]


def env_vars() -> dict[str, dict[str, str]]:
    """Every env-var name a provisioner may have to handle, and where it goes.

    NAMES AND PLACEMENTS ONLY -- no value, no default, ever (see the module
    docstring). The name set is OPERATIONAL_KEYS (the placement surface
    proper) plus _ALWAYS_SYNCED (the credential and identity vars pushed on
    every sync); GITHUB_TARGET_REPO is in both and appears once.
    """
    names = set(OPERATIONAL_KEYS) | set(deploy._ALWAYS_SYNCED)
    return {name: {"placement": _placement(name)} for name in sorted(names)}


def providers() -> dict[str, dict[str, str]]:
    """Each provider's credential var, model var, and key-slot column.

    KEY_INDEX_COLUMNS is indexed rather than .get()'d on purpose: a provider
    added to PROVIDERS without a key-index column should fail here, loudly,
    rather than publish a provider entry a consumer cannot act on.
    """
    return {
        provider: {
            "credential_var": credential_var,
            "model_var": model_var,
            "key_index_column": registry.KEY_INDEX_COLUMNS[provider],
        }
        for provider, (credential_var, model_var) in sorted(registry.PROVIDERS.items())
    }


def runtime_config() -> dict[str, object]:
    """runtime_config's columns, split by who is responsible for each.

    provisioner_required is derived by SUBTRACTION, not hand-listed: a
    column this repository can fill for itself is one of the backfilled
    ones (a declared non-None default), the one deliberately left NULL
    (NO_DEFAULT_BY_DESIGN), or a key-index column. Whatever is left is, by
    construction, something only the provisioner knows -- so adding a
    column with a default moves it into bot_backfilled automatically, and
    adding one without a default surfaces it as a new provisioner
    obligation that tests/test_provisioning_contract.py then makes someone
    justify against main.py's boot gate.

    provisioner_required_one_of is separate because exactly ONE of the
    three *_key_index columns is written -- whichever provider the visitor
    chose. The other two are legitimately NULL, so a flat required-list
    would be wrong in both directions (spec section 6.1).
    """
    by_name = dict(store.RUNTIME_CONFIG_COLUMNS)
    defaults = runtime_config_defaults.declared_defaults()
    one_of = sorted(registry.KEY_INDEX_COLUMNS.values())
    no_default = sorted(runtime_config_defaults.NO_DEFAULT_BY_DESIGN)
    bot_owned = set(defaults) | set(no_default) | set(one_of)
    return {
        "provisioner_required": [
            name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS if name not in bot_owned
        ],
        "provisioner_required_one_of": one_of,
        "bot_backfilled": [
            {"column": name, "sql_type": by_name[name], "default": defaults[name]}
            for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS
            if name in defaults
        ],
        "no_default_by_design": no_default,
    }


def slot_config() -> dict[str, list[str]]:
    """slot_config's columns, split the same way -- see _SLOT_CONFIG_OPTIONAL."""
    declared = [name for name, _sql_type in store.SLOT_CONFIG_COLUMNS]
    unknown = set(_SLOT_CONFIG_OPTIONAL) - set(declared)
    if unknown:
        raise ValueError(
            f"_SLOT_CONFIG_OPTIONAL names no such slot_config column: {sorted(unknown)}"
        )
    return {
        "provisioner_required": [n for n in declared if n not in _SLOT_CONFIG_OPTIONAL],
        "optional": [n for n in declared if n in _SLOT_CONFIG_OPTIONAL],
    }


def build_contract() -> dict[str, object]:
    """The whole contract, in the key order it is serialized in."""
    return {
        "generated_by": GENERATED_BY,
        "contract_version": CONTRACT_VERSION,
        "env_vars": env_vars(),
        "providers": providers(),
        "runtime_config": runtime_config(),
        "slot_config": slot_config(),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: PASS, all 15 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green. Nothing existing should change — this task adds two files and modifies none.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_contract.py tests/test_provisioning_contract.py
git commit -m "Derive the provisioning contract's content from the bot's own constants"
```

---

### Task 2: Serialize it, and commit the artifact

`build_contract()` produces a dict; the vendored artifact is bytes. This task adds the writer, generates `contracts/provisioning.json`, and adds the byte-identity check that spec §6.1 lists first — the local equivalent of the CI gate Task 3 wires up, so a stale contract fails a developer's own `pytest` run rather than only their PR.

Note in passing: `render.yaml`'s `buildFilter.ignoredPaths` is `**/*.md`, so a regenerated `contracts/provisioning.json` will trigger a Render redeploy. That is correct and deliberate — the contract only changes when the schema or the env-var surface changed, which is a deploy-worthy change anyway. Do not add an ignore entry for it.

**Files:**
- Modify: `scripts/gen_contract.py` (append the writer and CLI)
- Create: `contracts/provisioning.json` (generated — never hand-edited)
- Modify: `tests/test_provisioning_contract.py` (append)

**Interfaces:**
- Consumes: `gen_contract.build_contract()`, `CONTRACT_PATH`, `GENERATED_BY` (Task 1).
- Produces:
  - `scripts.gen_contract.render() -> str` — the contract's exact committed text, trailing newline included.
  - `scripts.gen_contract.write_contract(root: Path) -> Path` — writes `root / CONTRACT_PATH`, returns it.
  - `scripts.gen_contract.main(argv: list[str] | None = None) -> int` — `--root` defaulting to `"."`.
  - `contracts/provisioning.json` — the committed artifact Task 3's CI step and Task 4's assertions both read.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provisioning_contract.py` (and add `import json` plus `from pathlib import Path` to its stdlib imports, above the `import pytest` line):

```python
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _committed_contract() -> dict:
    path = _REPO_ROOT / gen_contract.CONTRACT_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def test_render_round_trips_through_json_and_ends_with_a_newline():
    text = gen_contract.render()
    assert text.endswith("\n")
    assert json.loads(text) == gen_contract.build_contract()


def test_render_is_deterministic_byte_for_byte():
    assert gen_contract.render() == gen_contract.render()


def test_write_contract_writes_exactly_one_file_at_the_fixed_path(tmp_path):
    written = gen_contract.write_contract(tmp_path)
    assert written == tmp_path / gen_contract.CONTRACT_PATH
    produced = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert produced == {gen_contract.CONTRACT_PATH}


def test_write_contract_is_idempotent_byte_for_byte(tmp_path):
    """The CI freshness job compares bytes, so a second run that differs at
    all -- a timestamp, a reordered set -- is a permanent red build."""
    first = gen_contract.write_contract(tmp_path).read_bytes()
    second = gen_contract.write_contract(tmp_path).read_bytes()
    assert first == second


def test_the_written_file_uses_lf_endings(tmp_path):
    """.gitattributes pins the working tree to LF. A CRLF write on Windows
    would fail the freshness check on that operator's machine and nowhere
    else."""
    assert b"\r\n" not in gen_contract.write_contract(tmp_path).read_bytes()


def test_every_file_call_in_gen_contract_declares_encoding_and_newline():
    """A missing explicit encoding= falls back to the OS locale encoding
    (cp1252 on Windows) and a missing newline= on a write falls back to
    CRLF there -- either fails the byte-for-byte freshness check on that
    operator's machine only. Parsed with ast, so a multi-line call or
    different quoting still gets caught. Mirrors the same guard in
    tests/test_gen_docs.py."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gen_contract))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in {"open", "read_text", "write_text"}:
            calls.append((name, node))
    assert calls, "expected at least one open/read_text/write_text call to check"
    for name, node in calls:
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        assert "encoding" in kwargs, f"{name}() at gen_contract.py:{node.lineno} has no encoding="
        if name == "write_text":
            assert "newline" in kwargs, f"{name}() at gen_contract.py:{node.lineno} has no newline="


def test_the_committed_contract_is_byte_identical_to_what_the_generator_produces():
    """Spec section 6.1's first blocking bot-side check, run locally so a
    stale contract fails `pytest` and not only CI. If this fails, run:
    uv run python -m scripts.gen_contract"""
    committed = (_REPO_ROOT / gen_contract.CONTRACT_PATH).read_text(encoding="utf-8")
    assert committed == gen_contract.render(), (
        f"{gen_contract.CONTRACT_PATH} is stale -- run scripts.gen_contract"
    )


def test_the_committed_contract_carries_the_do_not_edit_marker():
    contract = _committed_contract()
    assert contract["generated_by"] == gen_contract.GENERATED_BY
    assert contract["contract_version"] == gen_contract.CONTRACT_VERSION


def test_main_writes_and_reports(tmp_path, capsys):
    assert gen_contract.main(["--root", str(tmp_path)]) == 0
    assert gen_contract.CONTRACT_PATH in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: FAIL — `AttributeError: module 'scripts.gen_contract' has no attribute 'render'`, and `FileNotFoundError` for `contracts/provisioning.json`.

- [ ] **Step 3: Add the writer and CLI**

Add to `scripts/gen_contract.py` — the three new stdlib imports (`import argparse`, `import json`, `from pathlib import Path`) go at the top, above the `from config import ...` block; the rest appends after `build_contract()`:

```python
def render() -> str:
    """The contract's exact committed text.

    indent=2 for a readable git diff (the whole point of committing a
    generated file is that a reviewer can see what changed). sort_keys is
    deliberately OFF: every dict here is already built in a fixed order --
    sorted() where derived from a set, declared-tuple order where derived
    from the schema -- and sorting again would scramble
    runtime_config's blocks out of their meaningful order. The trailing
    newline keeps the file POSIX-clean so `git diff` has nothing to say
    about its last line.
    """
    return json.dumps(build_contract(), indent=2, sort_keys=False) + "\n"


def write_contract(root: Path) -> Path:
    """Write the contract under `root` and return the path.

    A REPLACING writer, confined to CONTRACT_PATH: the file has no
    hand-written content to preserve, carries a do-not-edit marker, and CI
    fails on drift. `root` itself is caller-supplied (main()'s --root) so
    the tests can point this at a scratch directory -- that's a guarantee
    about the file NAME, not a sandbox around `root`, exactly as
    gen_docs.write_all() documents.
    """
    path = root / CONTRACT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate contracts/provisioning.json from the code"
    )
    parser.add_argument("--root", default=".", help="repository root to write under")
    args = parser.parse_args(argv)
    # as_posix(), matching gen_docs.main(): the printed path follows whatever
    # --root was given (relative for the default "."), so a normal run prints
    # exactly "wrote contracts/provisioning.json". Do NOT relative_to(root)
    # here -- Path("contracts/x").relative_to(".") raises ValueError.
    print(f"wrote {write_contract(Path(args.root)).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Generate the artifact**

Run: `uv run python -m scripts.gen_contract`
Expected: `wrote contracts/provisioning.json`

- [ ] **Step 5: Eyeball the generated file once**

Run: `cat contracts/provisioning.json`

Confirm by reading, before committing it (this is the one manual review the artifact ever gets):
- `"generated_by"` and `"contract_version": 1` are the first two keys.
- `env_vars` has 33 entries (24 `OPERATIONAL_KEYS` + 10 `_ALWAYS_SYNCED`, `GITHUB_TARGET_REPO` counted once), each `{"placement": ...}` and nothing else.
- **No entry anywhere holds a credential-looking value.** Every `env_vars` entry is a name plus a placement word; the only values in the whole file are `runtime_config.bot_backfilled` defaults (numbers, `false`, and the string `"04:00:00"`) and column/provider/env-var *names*. If any string in this file looks like a key, a URL with credentials, or a PEM fragment, **stop, do not commit, and report it** — per `CLAUDE.md`'s secret-handling section, name which value appeared and recommend rotation.
- `runtime_config.provisioner_required` is `["id", "provider", "updated_at"]`.
- `slot_config.optional` is the two `vertex_gcp_*` columns.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add scripts/gen_contract.py contracts/provisioning.json tests/test_provisioning_contract.py
git commit -m "Generate and commit contracts/provisioning.json"
```

---

### Task 3: The CI freshness gate

Spec §5.4: a step in CI's **existing** `docs` job, using the same `git add` / `git diff --cached --exit-code` byte-compare that keeps `guide/reference/` honest. That job deliberately has no Postgres service, and `gen_contract` — like `gen_docs` — reads class metadata and module constants only, so it fits there unchanged. This is the bot's only *blocking* contract check and it is purely local: no sibling checkout, no pin, no network.

The existing workflow tests in `tests/test_ci_workflow.py` assert structure rather than running CI; extend them the same way. Watch the ordering assertions: `test_the_docs_job_regenerates_before_diffing` finds the *first* step whose `run` contains `git diff --cached --exit-code`, so the new steps must be appended after the existing docs pair, not interleaved.

**Files:**
- Modify: `.github/workflows/ci.yml:41-63` (the `docs` job)
- Modify: `tests/test_ci_workflow.py`

**Interfaces:**
- Consumes: `scripts.gen_contract`'s `python -m` entry point and `contracts/provisioning.json` (Task 2).
- Produces: nothing importable — a CI gate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ci_workflow.py`:

```python
def test_the_docs_job_regenerates_the_provisioning_contract_and_fails_on_drift():
    """Spec section 5.4: the same byte-compare that keeps guide/reference/
    honest, applied to the contract another repository vendors verbatim."""
    steps = _workflow()["jobs"]["docs"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.gen_contract" in commands
    assert "git add -A contracts/" in commands
    assert "git diff --cached --exit-code contracts/" in commands


def test_the_contract_is_regenerated_before_it_is_diffed():
    """Same reasoning as test_the_docs_job_regenerates_before_diffing: a
    diff-then-regenerate reordering would never observe drift, and a
    substring check on the joined commands cannot tell the orderings apart."""
    steps = _workflow()["jobs"]["docs"]["steps"]
    regenerate_index = next(
        i for i, step in enumerate(steps) if "scripts.gen_contract" in step.get("run", "")
    )
    diff_index = next(
        i
        for i, step in enumerate(steps)
        if "contracts/" in step.get("run", "")
        and "git diff --cached --exit-code" in step.get("run", "")
    )
    assert regenerate_index < diff_index


def test_the_contract_freshness_check_needs_no_database_or_sibling_checkout():
    """The bot's only blocking contract check is purely local: gen_contract
    reads class metadata and module constants, so no Postgres service -- and
    no sibling repository checkout, no pinned ref, no network (spec 5.4)."""
    job = _workflow()["jobs"]["docs"]
    assert "services" not in job
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "onboarding-wizard" not in text, (
        "the bot's blocking contract check must not depend on the consumer repo "
        "-- the advisory cross-repo job is a separate, later stage (spec 6.2)"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ci_workflow.py -v`
Expected: FAIL — `StopIteration` / assertion errors on the three new tests; the seven existing ones still pass.

- [ ] **Step 3: Add the steps to the `docs` job**

In `.github/workflows/ci.yml`, replace the `docs` job's comment and two steps (lines 55-63) with:

```yaml
      # No Postgres service: gen_docs and gen_contract both read class
      # metadata and module constants only, never the database and never a
      # configured value.
      - name: Regenerate reference docs
        run: uv run python -m scripts.gen_docs

      - name: Fail if the generated docs are stale
        run: |
          git add -A guide/reference/
          git diff --cached --exit-code guide/reference/

      # The provisioning contract is vendored verbatim into the
      # onboarding-wizard repository, so a stale copy here silently
      # misinforms the other repo's parity tests. This check is purely
      # local -- no sibling checkout, no pinned ref, no network -- because
      # the producer must never be blocked on the consumer.
      - name: Regenerate the provisioning contract
        run: uv run python -m scripts.gen_contract

      - name: Fail if the provisioning contract is stale
        run: |
          git add -A contracts/
          git diff --cached --exit-code contracts/
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ci_workflow.py -v`
Expected: PASS, all ten tests including the four pre-existing `docs`-job ones.

- [ ] **Step 5: Prove the gate actually fires**

Run the two CI commands by hand against a deliberately drifted file, so the gate is observed failing and then observed passing. Not a test — a one-off manual verification that the byte-compare is real.

First, drift the committed file *without* regenerating (this is what a forgotten `gen_contract` run looks like to CI):

```bash
python3 - <<'PY'
from pathlib import Path
p = Path("contracts/provisioning.json")
p.write_text(
    p.read_text(encoding="utf-8").replace('"contract_version": 1', '"contract_version": 99'),
    encoding="utf-8", newline="\n",
)
PY
git add -A contracts/
git diff --cached --exit-code contracts/ ; echo "exit=$?"
```

Expected: a diff is printed and `exit=1` — the gate fires.

Then restore and confirm the gate goes quiet:

```bash
git reset contracts/
uv run python -m scripts.gen_contract
git add -A contracts/
git diff --cached --exit-code contracts/ ; echo "exit=$?"
git reset contracts/
```

Expected: no diff and `exit=0`. Finish with `git status` reporting a clean `contracts/` before continuing — if it is not clean, the regeneration is not deterministic and that is a Task 2 defect: **stop and report**.

- [ ] **Step 6: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ci.yml tests/test_ci_workflow.py
git commit -m "Fail CI's docs job when the provisioning contract is stale"
```

---

### Task 4: The contract cannot lie about ownership

The remaining three blocking bot-side checks from spec §6.1. Byte-identity (Task 2) proves the file matches the generator; these prove the generator's claims match reality:

1. **`provisioner_required` is exactly what `main.py`'s boot gate demands** — so the contract cannot claim the wizard owns something the bot actually backfills, or vice versa. Two halves: a static one here, and a behavioural one in `tests/test_main_lifespan.py`, which is where the lifespan harness (`_env`: webhook secret, installation id, target repo, seeded `slot_config`) already lives. Duplicating that ~30-line autouse fixture into a second file to keep both halves in one place would be worse than the split; the static test's docstring names its behavioural partners so a reader finds them.
2. **Every `RUNTIME_CONFIG_COLUMNS` entry is nullable or defaulted, except `provisioner_required` members** — `store.init_pool()` widens with `ADD COLUMN IF NOT EXISTS`, which fails outright against a populated table for a `NOT NULL` column with no `DEFAULT`. `tests/test_store_schema.py::test_every_declared_column_is_nullable_or_defaulted_or_provisioner_written` already checks this against a *hand-typed* exemption set; the version here anchors the exemption to the contract's own derived list, which is the thing that has to be right. Leave the existing test in place — it also covers `slot_config` and holds without the contract.
3. **Every `db_only` key is absent from `render.yaml`** — a `db_only` key with a Render env-var entry is a second source of truth for a value the dispatcher only ever reads from `runtime_config` (ISSUES.md's 2026-08-17 "two sources of truth" entry).

**Files:**
- Modify: `tests/test_provisioning_contract.py` (append)
- Modify: `tests/test_main_lifespan.py` (append one test)
- Read (do not modify): `main.py:103-163`, `render.yaml`, `review_queue/store.py:44-67`

**Interfaces:**
- Consumes: `contracts/provisioning.json` (Task 2), `gen_contract.CONTRACT_PATH`, the existing `_committed_contract()` helper from Task 2, and `tests/test_main_lifespan.py`'s existing `_env` fixture and `_hang_forever` helper.
- Produces: nothing importable — blocking tests.

- [ ] **Step 1: Write the failing static tests**

Append to `tests/test_provisioning_contract.py` (add `import yaml` to its imports — already a dev dependency, used by `tests/test_ci_workflow.py`):

```python
def test_no_bot_backfilled_column_is_also_provisioner_required():
    """The contract's central claim. A column this repository fills at boot
    must never be presented to a provisioner as its responsibility -- that
    is how the wizard came to hand-copy 15 default values whose only guard
    was a pinned test in the other repo (spec section 1)."""
    block = _committed_contract()["runtime_config"]
    backfilled = {entry["column"] for entry in block["bot_backfilled"]}
    overlap = backfilled & set(block["provisioner_required"])
    assert not overlap, f"claimed as the provisioner's but backfilled here: {sorted(overlap)}"


def test_provisioner_required_is_exactly_the_boot_gate_s_three_columns():
    """main.py's lifespan refuses to start on exactly one runtime_config
    VALUE it cannot derive -- `provider` (main.py:109-116), because the bot
    cannot invent which provider a visitor chose. `id` and `updated_at` are
    the row's own identity: `id` is the CHECK-pinned singleton primary key,
    and `updated_at` is NOT NULL with no DEFAULT, so whoever INSERTs the row
    writes both in that same statement or the INSERT fails.

    The behavioural halves of this assertion live in
    tests/test_main_lifespan.py, where the lifespan harness already is:
    test_lifespan_refuses_to_start_without_provider (necessity) and
    test_a_row_with_only_the_contract_required_columns_boots (sufficiency).
    """
    block = _committed_contract()["runtime_config"]
    assert block["provisioner_required"] == ["id", "provider", "updated_at"]
    assert "provider" in block["provisioner_required"]


def test_the_boot_gate_reads_the_provider_and_slot_columns_the_contract_names():
    """A cheap staleness guard on the docstring above: if main.py's gate
    stops reading the provider override or the slot_config row, the claim
    that provisioner_required mirrors it has quietly become false."""
    source = (_REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "store.get_provider_override()" in source
    assert "store.get_slot_config(" in source


def test_every_declared_column_the_contract_does_not_require_can_be_added_later():
    """store.init_pool() widens a live table with ADD COLUMN IF NOT EXISTS,
    which fails outright against a non-empty table for a NOT NULL column
    carrying no DEFAULT. The provisioner_required columns are exempt because
    they are never absent -- whoever creates the row writes them in the same
    statement (spec section 3.1). Anchored on the contract's derived list
    rather than a hand-typed one, so a new column added without a default
    fails here instead of at a customer's boot."""
    required = set(_committed_contract()["runtime_config"]["provisioner_required"])
    for name, sql_type in store.RUNTIME_CONFIG_COLUMNS:
        if name in required:
            continue
        upper = sql_type.upper()
        assert "NOT NULL" not in upper or "DEFAULT" in upper, (
            f"{name} is NOT NULL with no DEFAULT and is not provisioner_required -- "
            "ADD COLUMN IF NOT EXISTS cannot add it to a table that already has rows"
        )


def test_no_db_only_key_has_a_render_yaml_entry():
    """A db_only key with a Render env var is a second source of truth for a
    value the dispatcher only ever reads from runtime_config -- an operator
    edits the Render var, redeploys, and cannot explain the non-effect
    (ISSUES.md 2026-08-17, "two sources of truth")."""
    render_yaml = yaml.safe_load((_REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    declared = {
        var["key"]
        for service in render_yaml["services"]
        for var in service.get("envVars", [])
    }
    assert declared, "render.yaml declares no env vars -- this check would be vacuous"
    db_only = {
        name
        for name, entry in _committed_contract()["env_vars"].items()
        if entry["placement"] == "db_only"
    }
    assert db_only, "the contract lists no db_only keys -- this check would be vacuous"
    overlap = declared & db_only
    assert not overlap, f"db_only keys must never be Render env vars: {sorted(overlap)}"


def test_the_always_synced_placement_matches_deploys_own_tuple():
    """The mirror of the check above, so the placement labels are
    load-bearing in both directions: the COMMITTED file (not just the
    generator) must still agree with _ALWAYS_SYNCED. Deliberately not
    compared against render.yaml -- render.yaml declares only the subset
    Render needs pre-declared, while _wanted_env() pushes more
    (GITHUB_APP_INSTALLATION_ID, the active provider's credential), so a
    render.yaml equality check here would be false by design."""
    always = {
        name
        for name, entry in _committed_contract()["env_vars"].items()
        if entry["placement"] == "always_synced"
    }
    assert always == set(deploy._ALWAYS_SYNCED)
```

- [ ] **Step 2: Run them — every one must pass on the first run**

Run: `uv run pytest tests/test_provisioning_contract.py -v`
Expected: PASS, all 30 tests.

There is no red-green cycle here and pretending to one would be dishonest: these are *conformance* assertions, checking Task 1's derivations against three sources the generator never reads — `main.py`'s boot gate, the DDL's own `NOT NULL`/`DEFAULT` text, and `render.yaml`. Nothing new is being implemented, so the only correct outcome is green.

**A failure here is a real defect, not a missing implementation. Stop and report rather than fixing it**, and say which of these it is:
- `test_no_bot_backfilled_column_is_also_provisioner_required` or `test_provisioner_required_is_exactly_the_boot_gate_s_three_columns` red → Task 1's subtraction is wrong, or a column was added to `RUNTIME_CONFIG_COLUMNS` without a default.
- `test_every_declared_column_the_contract_does_not_require_can_be_added_later` red → a live `store.init_pool()` widening would raise `NotNullViolation` against a populated table. A schema bug, not a test bug.
- `test_no_db_only_key_has_a_render_yaml_entry` red → a genuine second source of truth exists today.

Never resolve any of these by loosening an assertion or hand-editing `contracts/provisioning.json`.

- [ ] **Step 3: Write the behavioural test**

Same framing as Step 2: this pins behaviour Stage 1 already delivers against the contract Task 2 committed, so it must pass on its first run.

Append to `tests/test_main_lifespan.py`:

```python
async def test_a_row_with_only_the_contract_required_columns_boots(
    monkeypatch, db_exec, db_query
):
    """Sufficiency half of contracts/provisioning.json's central claim: a
    runtime_config row carrying ONLY provisioner_required plus one
    provisioner_required_one_of column -- exactly what the contract tells a
    provisioner to write, and nothing more -- must boot. If it does not, the
    contract is understating what the provisioner owes and the 2026-09-09
    incident (18 of 22 columns NULL forever) is reachable again.

    The necessity half is test_lifespan_refuses_to_start_without_provider
    above; the static half is
    tests/test_provisioning_contract.py::test_provisioner_required_is_exactly_
    the_boot_gate_s_three_columns.
    """
    import json
    from pathlib import Path

    contract = json.loads(
        (Path(__file__).resolve().parent.parent / "contracts" / "provisioning.json")
        .read_text(encoding="utf-8")
    )
    required = contract["runtime_config"]["provisioner_required"]
    assert required == ["id", "provider", "updated_at"]
    one_of = contract["runtime_config"]["provisioner_required_one_of"]
    assert "gemini_key_index" in one_of

    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", lambda: 12345)

    # Exactly what a minimal provisioner writes: the required columns plus
    # the chosen provider's key-slot index. Every other column is left
    # absent, i.e. NULL, for store.init_pool()'s backfill to fill.
    db_exec("DELETE FROM runtime_config WHERE id = 1")
    db_exec(
        "INSERT INTO runtime_config (id, provider, updated_at, gemini_key_index) "
        "VALUES (1, 'gemini', '2026-01-01T00:00:00+00:00', 0)"
    )

    async with main.lifespan(main.app):
        pass

    # Distinguishes a working backfill from a row that was never actually
    # narrow: every bot_backfilled column must have come back non-NULL.
    # key_usage_token_cap is correctly absent from that list -- it is in
    # no_default_by_design, and a NULL cap is "intentionally disabled".
    backfilled = [entry["column"] for entry in contract["runtime_config"]["bot_backfilled"]]
    row = db_query(f"SELECT {', '.join(backfilled)} FROM runtime_config WHERE id = 1")[0]
    still_null = [name for name, value in zip(backfilled, row) if value is None]
    assert not still_null, f"backfill left these NULL: {still_null}"
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: PASS, including the new test and all pre-existing ones.

This test asserts behaviour Stage 1 already delivers, so it should pass on the first run. If it fails, **stop and report** rather than changing `store.py` or `main.py` — Stage 1 is merged and out of this plan's scope, and a failure here means the contract's `provisioner_required` derivation (Task 1) is wrong, or Stage 1 has a real defect.

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_provisioning_contract.py tests/test_main_lifespan.py
git commit -m "Pin the provisioning contract against the boot gate, the schema, and render.yaml"
```

---

## Done criteria

- [ ] `uv run pytest -v` fully green; `uv run ruff check .` clean.
- [ ] `uv run python -m scripts.gen_contract` writes `contracts/provisioning.json` and leaves a clean `git status` on a fresh checkout.
- [ ] `scripts/gen_contract.py` imports the `Settings` class only (transitively, via `runtime_config_defaults`) and never `config.settings` — pinned both by AST and by the sentinel test.
- [ ] The committed `contracts/provisioning.json` contains no value that is not a name, a placement word, a SQL type, or a non-secret operational default.
- [ ] CI's `docs` job regenerates the contract and fails on drift, with no Postgres service, no sibling checkout, and no network.
- [ ] `provisioner_required` is `["id", "provider", "updated_at"]`, derived by subtraction, and a row carrying only those plus one `*_key_index` boots successfully with every backfilled column non-NULL afterwards.
- [ ] No `db_only` key appears in `render.yaml`.
- [ ] **Not done here, deliberately:** no push (`CLAUDE.md` requires the `deploy-verify` skill before any push to `main`); nothing in the `onboarding-wizard` repository (spec §7 and the wizard-side half of §6.1 — Stage 3); no advisory cross-repo job (§6.2 — Stage 4); no `CLAUDE.md` section (§9 — Stage 5); and the spec §10 failure-path validation (add a throwaway column, confirm the freshness gate goes red and the advisory job reports a lagging wizard) is only half-runnable until Stage 4 exists — the freshness-gate half is exercised by Task 3 Step 5.
