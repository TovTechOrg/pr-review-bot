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
