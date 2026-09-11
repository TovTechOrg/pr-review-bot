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

import json
from pathlib import Path

import pytest
import yaml

from config import OPERATIONAL_KEYS, settings
from providers import registry
from review_queue import runtime_config_defaults, store
from scripts import deploy, gen_contract

SENTINEL = "SENTINEL-3f0c71ba9d42e6c8-MUST-NOT-BE-VENDORED"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _committed_contract() -> dict:
    path = _REPO_ROOT / gen_contract.CONTRACT_PATH
    return json.loads(path.read_text(encoding="utf-8"))


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


def test_build_contract_carries_the_marker_version_and_all_five_blocks():
    contract = gen_contract.build_contract()
    assert contract["generated_by"] == gen_contract.GENERATED_BY
    assert "do not edit" in contract["generated_by"].lower()
    assert "scripts.gen_contract" in contract["generated_by"]
    assert contract["contract_version"] == gen_contract.CONTRACT_VERSION
    assert set(contract) == {
        "generated_by", "contract_version",
        "env_vars", "providers", "model_validation", "runtime_config", "slot_config",
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
    """store._widening_safe_sql_type already strips a bare NOT NULL for
    EVERY column ADD COLUMN IF NOT EXISTS widens (Stage 1), so this specific
    NotNullViolation cannot actually reach a live database any more -- this
    test instead pins the DECLARATION-level invariant that specific
    workaround exists to make unnecessary: a column outside
    provisioner_required should never need a NOT-NULL-no-DEFAULT type in the
    first place, since nothing guarantees the provisioner wrote it. Anchored
    on the contract's derived list rather than a hand-typed one, so a new
    column added this way is flagged here rather than only being silently
    rescued by the widening relaxation."""
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
