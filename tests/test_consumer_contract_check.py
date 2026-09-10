"""scripts/check_consumer_contract.py -- comparing this repo's contract
against a consumer's vendored copy.

Pure-function coverage only in this file's Task 1 half: compare(),
differences(), and render_report() take strings and dicts, never touch the
network or the filesystem, and never shell out. See
docs/superpowers/specs/2026-09-10-cross-repo-contract-direction-design.md
section 6.2 and docs/superpowers/plans/2026-09-10-cross-repo-advisory-consumer-lag.md.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import check_consumer_contract as ccc
from scripts import gen_contract

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _contract(**overrides: object) -> dict:
    base = {
        "generated_by": "scripts.gen_contract -- do not edit by hand",
        "contract_version": 1,
        "env_vars": {
            "GITHUB_TARGET_REPO": {"placement": "always_synced"},
            "KEY_USAGE_TOKEN_CAP": {"placement": "db_only"},
        },
        "providers": {
            "gemini": {
                "credential_var": "GEMINI_API_KEY",
                "model_var": "GEMINI_MODEL",
                "key_index_column": "gemini_key_index",
            },
        },
        "runtime_config": {
            "provisioner_required": ["id", "provider", "updated_at"],
            "provisioner_required_one_of": ["gemini_key_index"],
            "bot_backfilled": [
                {
                    "column": "cooldown_base_seconds",
                    "sql_type": "DOUBLE PRECISION",
                    "default": 300.0,
                },
            ],
            "no_default_by_design": ["key_usage_token_cap"],
        },
        "slot_config": {
            "provisioner_required": ["provider", "slot_index", "model", "updated_at"],
            "optional": ["vertex_gcp_project", "vertex_gcp_location"],
        },
    }
    base.update(overrides)
    return base


def _text(contract: dict) -> str:
    return json.dumps(contract, indent=2) + "\n"


def test_identical_text_is_in_sync():
    text = _text(_contract())
    report = ccc.compare(text, text)
    assert report.verdict == ccc.IN_SYNC


def test_an_added_env_var_is_reported_by_name():
    bot = _contract()
    bot["env_vars"]["NEW_VAR"] = {"placement": "always_synced"}
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    assert any("env_vars.NEW_VAR" in d for d in report.details)


def test_a_changed_placement_names_both_the_old_and_the_new_value():
    bot = _contract()
    bot["env_vars"]["GITHUB_TARGET_REPO"] = {"placement": "db_only"}
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    detail = next(d for d in report.details if "env_vars.GITHUB_TARGET_REPO.placement" in d)
    assert "db_only" in detail
    assert "always_synced" in detail


def test_a_renamed_env_var_reports_both_the_addition_and_the_removal():
    bot = _contract()
    bot["env_vars"]["GITHUB_TRACKED_REPOS"] = bot["env_vars"].pop("GITHUB_TARGET_REPO")
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    joined = "\n".join(report.details)
    assert "env_vars.GITHUB_TRACKED_REPOS" in joined
    assert "env_vars.GITHUB_TARGET_REPO" in joined


def test_a_changed_backfilled_default_names_the_column_and_both_values():
    bot = _contract()
    bot["runtime_config"]["bot_backfilled"][0]["default"] = 600.0
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    detail = next(
        d for d in report.details
        if "runtime_config.bot_backfilled[cooldown_base_seconds].default" in d
    )
    assert "600.0" in detail
    assert "300.0" in detail


def test_an_added_backfilled_column_is_keyed_by_column_name_not_list_index():
    bot = _contract()
    bot["runtime_config"]["bot_backfilled"].insert(
        0, {"column": "aaa_new_column", "sql_type": "TEXT", "default": "x"}
    )
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    joined = "\n".join(report.details)
    assert "aaa_new_column" in joined
    assert "cooldown_base_seconds" not in joined


def test_a_changed_provisioner_required_list_is_reported_as_one_ordered_leaf():
    bot = _contract()
    bot["runtime_config"]["provisioner_required"] = ["provider", "id", "updated_at"]
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    matching = [d for d in report.details if "provisioner_required" in d]
    assert len(matching) == 1


def test_a_contract_version_mismatch_leads_the_report():
    bot = _contract(contract_version=2)
    report = ccc.compare(_text(bot), _text(_contract()))
    assert report.verdict == ccc.LAGGING
    assert "contract_version" in report.headline
    assert "2" in report.headline


def test_a_missing_vendored_copy_is_lagging_not_an_error():
    report = ccc.compare(_text(_contract()), None)
    assert report.verdict == ccc.LAGGING
    assert "has not vendored" in report.headline


def test_a_malformed_vendored_copy_is_lagging_not_uncheckable():
    report = ccc.compare(_text(_contract()), "{not json")
    assert report.verdict == ccc.LAGGING
    assert "not valid JSON" in report.headline


def test_a_malformed_bot_contract_is_uncheckable():
    report = ccc.compare("{not json", _text(_contract()))
    assert report.verdict == ccc.UNCHECKABLE


def test_byte_drift_with_no_semantic_difference_is_still_lagging():
    bot_text = json.dumps(_contract(), indent=2) + "\n"
    consumer_text = json.dumps(_contract(), indent=0) + "\n"
    report = ccc.compare(bot_text, consumer_text)
    assert report.verdict == ccc.LAGGING
    assert not report.details
    assert "no field-level difference" in report.headline


def test_the_pin_never_changes_the_verdict():
    identical_text = _text(_contract())
    report = ccc.compare(identical_text, identical_text, pin="a" * 40, pin_context="way behind")
    assert report.verdict == ccc.IN_SYNC

    bot = _contract()
    bot["env_vars"]["EXTRA"] = {"placement": "db_only"}
    report = ccc.compare(_text(bot), _text(_contract()), pin=None, pin_context=None)
    assert report.verdict == ccc.LAGGING


def test_the_report_is_markdown_and_names_the_consumer_repo():
    report = ccc.compare(_text(_contract()), None)
    rendered = ccc.render_report(report)
    assert ccc.CONSUMER_REPO in rendered
    assert rendered.startswith("## ")


def test_the_report_tells_the_reader_which_command_closes_the_lag():
    report = ccc.compare(_text(_contract()), None)
    rendered = ccc.render_report(report)
    assert "update_bot_contract.py" in rendered


def test_an_in_sync_report_carries_no_what_closes_this_section():
    text = _text(_contract())
    report = ccc.compare(text, text)
    rendered = ccc.render_report(report)
    assert "What closes this" not in rendered


def test_exit_codes_map_one_to_one_onto_the_three_verdicts():
    assert ccc.EXIT_CODE == {ccc.IN_SYNC: 0, ccc.LAGGING: 1, ccc.UNCHECKABLE: 2}
    assert len(set(ccc.EXIT_CODE.values())) == 3


def test_check_consumer_contract_does_not_import_the_settings_instance():
    """Mirrors tests/test_provisioning_contract.py's equivalent guard --
    parsed with ast, not grepped, since a grep would also match this
    module's own docstring explaining the rule."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ccc))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            assert "settings" not in imported, (
                f"line {node.lineno} imports the settings instance from {node.module}"
            )


def test_the_real_committed_contract_compares_clean_against_itself():
    committed = (_REPO_ROOT / gen_contract.CONTRACT_PATH).read_text(encoding="utf-8")
    report = ccc.compare(committed, committed)
    assert report.verdict == ccc.IN_SYNC


# --- Task 2: load_consumer / pin_context / main ---------------------------


def _write_consumer(root: Path, contract: dict | None, pin: str | None) -> None:
    if contract is not None:
        path = root / ccc.CONSUMER_CONTRACT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_text(contract), encoding="utf-8")
    if pin is not None:
        pin_path = root / ccc.CONSUMER_PIN_PATH
        pin_path.parent.mkdir(parents=True, exist_ok=True)
        pin_path.write_text(pin + "\n", encoding="utf-8")


def test_a_missing_consumer_root_is_uncheckable(tmp_path):
    text, pin, reason = ccc.load_consumer(tmp_path / "does-not-exist")
    assert text is None
    assert pin is None
    assert reason is not None


def test_a_consumer_root_without_a_vendored_contract_is_lagging(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    text, pin, reason = ccc.load_consumer(tmp_path)
    assert text is None
    assert reason is None


def test_load_consumer_reads_the_pin_stripping_comments_and_blanks(tmp_path):
    _write_consumer(tmp_path, _contract(), None)
    pin_path = tmp_path / ccc.CONSUMER_PIN_PATH
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text("# held back deliberately, see ISSUES.md\n\n" + "a" * 40 + "\n")
    _text_, pin, _reason = ccc.load_consumer(tmp_path)
    assert pin == "a" * 40


def test_an_identical_vendored_contract_exits_zero(tmp_path):
    contract = _contract()
    _write_consumer(tmp_path, contract, None)
    exit_code = ccc.main([
        "--consumer-root", str(tmp_path),
        "--bot-contract", _text(contract),
        "--committed-contract", str(tmp_path / "nonexistent-so-no-staleness-check.json"),
        "--summary", str(tmp_path / "summary.md"),
    ])
    assert exit_code == 0


def test_never_fail_returns_zero_for_every_verdict(tmp_path):
    exit_code = ccc.main([
        "--consumer-root", str(tmp_path / "missing"),
        "--bot-contract", _text(_contract()),
        "--committed-contract", str(tmp_path / "nonexistent.json"),
        "--summary", str(tmp_path / "summary.md"),
        "--never-fail",
    ])
    assert exit_code == 0


def test_a_stale_committed_contract_in_this_repo_is_uncheckable(tmp_path):
    committed_path = tmp_path / "committed.json"
    committed_path.write_text(_text(_contract(contract_version=999)), encoding="utf-8")
    _write_consumer(tmp_path / "consumer", _contract(), None)
    exit_code = ccc.main([
        "--consumer-root", str(tmp_path / "consumer"),
        "--committed-contract", str(committed_path),
        "--summary", str(tmp_path / "summary.md"),
    ])
    assert exit_code == 2
    assert "stale" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_a_missing_committed_contract_in_this_repo_is_uncheckable(tmp_path):
    """Distinct from staleness: nothing to cross-check against at all is its
    own UNCHECKABLE reason, not a silently-skipped check. Only reachable
    when --bot-contract is NOT given, since a caller-supplied override has
    no committed file to cross-check against by construction."""
    _write_consumer(tmp_path / "consumer", _contract(), None)
    exit_code = ccc.main([
        "--consumer-root", str(tmp_path / "consumer"),
        "--committed-contract", str(tmp_path / "does-not-exist.json"),
        "--summary", str(tmp_path / "summary.md"),
    ])
    assert exit_code == 2
    assert "missing" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_main_writes_markdown_to_the_summary_path(tmp_path):
    summary_path = tmp_path / "summary.md"
    ccc.main([
        "--consumer-root", str(tmp_path / "missing"),
        "--bot-contract", _text(_contract()),
        "--committed-contract", str(tmp_path / "nonexistent.json"),
        "--summary", str(summary_path),
    ])
    written = summary_path.read_text(encoding="utf-8")
    assert ccc.CONSUMER_REPO in written


def test_main_never_raises_when_the_summary_path_is_unwritable(tmp_path):
    """The report was already printed to stdout; failing to ALSO append it
    to an unwritable summary path must not crash a script whose whole job
    is to always produce a report."""
    contract = _contract()
    _write_consumer(tmp_path / "consumer", contract, None)
    unwritable_summary = tmp_path / "no-such-directory" / "summary.md"
    exit_code = ccc.main([
        "--consumer-root", str(tmp_path / "consumer"),
        "--bot-contract", _text(contract),
        "--committed-contract", str(tmp_path / "nonexistent.json"),
        "--summary", str(unwritable_summary),
    ])
    assert exit_code == 0


def test_main_never_raises_on_garbage_input(tmp_path):
    a_directory_not_a_file = tmp_path / "a-directory"
    a_directory_not_a_file.mkdir()
    exit_code = ccc.main([
        "--consumer-root", str(tmp_path / "missing"),
        "--bot-contract", str(a_directory_not_a_file),  # nonsense on purpose
        "--committed-contract", str(tmp_path / "nonexistent.json"),
        "--summary", str(tmp_path / "summary.md"),
    ])
    assert exit_code in (1, 2)


def test_a_pin_that_is_not_forty_hex_characters_is_never_shelled_out(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called for a malformed pin")

    monkeypatch.setattr(ccc.subprocess, "run", _boom)
    result = ccc.pin_context("main; echo pwned", tmp_path)
    assert result is not None
    assert "40-character" in result


def test_a_pin_file_with_comment_lines_still_yields_the_sha(tmp_path):
    consumer_root = tmp_path / "consumer"
    _write_consumer(consumer_root, _contract(), None)
    pin_path = consumer_root / ccc.CONSUMER_PIN_PATH
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text("# a comment\n" + "b" * 40 + "\n", encoding="utf-8")
    _text_, pin, _reason = ccc.load_consumer(consumer_root)
    assert pin == "b" * 40


def test_pin_context_returns_none_when_git_fails(tmp_path):
    result = ccc.pin_context("c" * 40, tmp_path)
    assert result is None or "not reachable" in result


def test_pin_context_resolves_a_real_reachable_commit():
    first_commit = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, cwd=_REPO_ROOT, check=True,
    ).stdout.strip()
    result = ccc.pin_context(first_commit, _REPO_ROOT)
    assert result is not None
    assert "behind main" in result


def test_the_pin_is_absent_from_the_verdict_but_present_in_the_report(tmp_path):
    contract = _contract()
    consumer_root = tmp_path / "consumer"
    _write_consumer(consumer_root, contract, "d" * 40)
    exit_code = ccc.main([
        "--consumer-root", str(consumer_root),
        "--bot-contract", _text(contract),
        "--committed-contract", str(tmp_path / "nonexistent.json"),
        "--summary", str(tmp_path / "summary.md"),
    ])
    assert exit_code == 0
    written = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "d" * 40 in written
