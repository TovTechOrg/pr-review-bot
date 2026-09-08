"""gen_docs reads CLASS metadata, never the live settings instance.

That distinction is the whole safety story: Settings.model_fields carries
declared defaults, while config.settings carries this machine's real
credentials -- and everything generated here is committed and published.
"""
from __future__ import annotations

from pathlib import Path

from config import OPERATIONAL_KEYS, settings
from scripts import gen_docs

SENTINEL = "SENTINEL-6d21fa48c093be75-MUST-NOT-BE-PUBLISHED"


def test_config_table_lists_every_settings_field():
    from config import Settings

    table = gen_docs.render_config()
    for name in Settings.model_fields:
        assert name.upper() in table, f"{name} missing from the generated table"


def test_config_table_never_contains_a_configured_value(monkeypatch):
    """The regression guard for the rule this stage turns on. If a generator is
    ever changed to read `settings` instead of `Settings`, this fails."""
    for field in ("database_url", "github_webhook_secret", "groq_api_key",
                  "gemini_api_key", "vertex_gcp_service_account_key", "github_app_private_key"):
        monkeypatch.setattr(settings, field, SENTINEL, raising=False)
    assert SENTINEL not in gen_docs.render_config()


def test_no_generated_file_contains_a_configured_value(tmp_path, monkeypatch):
    """Same regression guard as test_config_table_never_contains_a_configured_value,
    extended to every renderer write_all() produces -- not just render_config().
    A future renderer that reads deploy.settings (the live instance deploy.py
    imports at module scope) would otherwise evade both the AST guard (which
    only inspects gen_docs.py's own imports) and a config-only sentinel."""
    for field in ("database_url", "github_webhook_secret", "groq_api_key",
                  "gemini_api_key", "vertex_gcp_service_account_key", "github_app_private_key"):
        monkeypatch.setattr(settings, field, SENTINEL, raising=False)
    for path in gen_docs.write_all(tmp_path):
        assert SENTINEL not in path.read_text(encoding="utf-8"), path


def test_gen_docs_module_does_not_import_the_settings_instance():
    """Static guard complementing the behavioural one above: importing the
    singleton at all is the mistake.

    Parsed with ast rather than grepped for a substring. A source grep would
    also match the module's own docstring explaining the rule -- a false
    positive that has already bitten this project once (ISSUES.md, Stage 2
    Task 4, where a docstring naming a forbidden function failed that task's
    own read-only source check).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gen_docs))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            assert "settings" not in imported, (
                f"line {node.lineno} imports the settings instance from {node.module}"
            )


def test_config_table_marks_where_each_key_belongs():
    table = gen_docs.render_config()
    for line in table.splitlines():
        if line.startswith("| `LLM_PROVIDER`"):
            assert ".env.config" in line
        if line.startswith("| `GROQ_API_KEY`"):
            assert ".env" in line and ".env.config" not in line
    assert "LLM_PROVIDER" in OPERATIONAL_KEYS


def test_generated_output_carries_the_do_not_edit_header():
    assert gen_docs.render_config().startswith(gen_docs.GENERATED_HEADER)
    assert "do not edit" in gen_docs.GENERATED_HEADER.lower()
    assert "scripts.gen_docs" in gen_docs.GENERATED_HEADER


def test_render_config_is_deterministic():
    """CI compares byte-for-byte, so any run-to-run variation is a red build."""
    assert gen_docs.render_config() == gen_docs.render_config()


def test_default_text_handles_a_field_with_no_declared_default():
    """Fix D: a required field (or a default_factory-only one) has no plain
    default at all -- field.default is pydantic's PydanticUndefined sentinel.
    No Settings field is like this today, so this test builds a throwaway
    local model rather than relying on one existing in config.Settings.
    Without the defensive branch, this would render the literal string
    "PydanticUndefined" into a published doc."""
    from pydantic import BaseModel, Field

    class _Throwaway(BaseModel):
        required_field: int
        factory_field: list[str] = Field(default_factory=list)

    text = gen_docs._default_text(_Throwaway.model_fields["required_field"])
    assert "PydanticUndefined" not in text
    assert "required" in text.lower()

    text = gen_docs._default_text(_Throwaway.model_fields["factory_field"])
    assert "PydanticUndefined" not in text


def test_pricing_table_carries_every_rate_with_its_provenance():
    from providers import pricing

    table = gen_docs.render_pricing()
    for (provider, model), rate in pricing._RATES.items():
        assert model in table
        assert provider in table
        assert rate.verified in table
        assert rate.source_url in table


def test_pricing_table_surfaces_an_inherited_rates_caveat():
    """A `verified` date that records no independent check must not be
    presented as though it did -- the note exists precisely to say so."""
    table = gen_docs.render_pricing()
    assert "not independently checked" in table


def test_pricing_table_explains_that_an_unpriced_model_still_runs():
    table = gen_docs.render_pricing()
    assert "without a cost estimate" in table


def test_sync_env_table_separates_pushed_from_never_pushed():
    from scripts import deploy

    table = gen_docs.render_sync_env()
    for name in deploy._ALWAYS_SYNCED:
        assert name in table
    for name in deploy._DB_SYNCED_OPERATIONAL_KEYS:
        assert name in table, "the DB-only keys must be listed as deliberately never pushed"
    for name in deploy._NEVER_SYNCED_OPERATIONAL_KEYS:
        assert name in table
    assert "runtime_config" in table, "must say WHERE the DB-only keys actually live"


def test_sync_env_table_explains_numbered_key_slots():
    """Fix B: the page must at least mention the numbered-slot mechanism
    (GEMINI_API_KEY_2, GROQ_API_KEY_3, ...) that _wanted_env() also pushes --
    without asserting how many slots exist or what they're set to, since that
    would require reading local configured values."""
    table = gen_docs.render_sync_env()
    assert "slot" in table.lower()
    assert "_2" in table


def test_sync_env_table_lists_every_providers_model_var():
    from providers import registry

    table = gen_docs.render_sync_env()
    for _credential, model_var in registry.PROVIDERS.values():
        assert model_var in table


def test_the_new_renderers_are_deterministic():
    assert gen_docs.render_pricing() == gen_docs.render_pricing()
    assert gen_docs.render_sync_env() == gen_docs.render_sync_env()


def test_escape_cell_escapes_a_literal_pipe():
    """Fix E: an unescaped `|` in a table cell would corrupt the row's column
    structure."""
    assert gen_docs._escape_cell("a | b") == "a \\| b"
    assert gen_docs._escape_cell("no pipes here") == "no pipes here"


def test_checks_table_escapes_a_verifies_string_containing_a_pipe(monkeypatch):
    """A `verifies` string with a literal `|` must not corrupt the table."""
    from scripts import deploy

    fake_check = deploy.CheckSpec(
        "fake-check", lambda: None, "verifies a | b", True
    )
    monkeypatch.setattr(deploy, "CHECKS", deploy.CHECKS + (fake_check,))
    table = gen_docs.render_checks()
    assert "verifies a \\| b" in table
    lines = [line for line in table.splitlines() if line.startswith("| `fake-check`")]
    assert len(lines) == 1
    # Column separators only: strip the escaped pipe first, since "\|" still
    # contains a literal "|" character that must not be mistaken for a
    # (fifth) column boundary.
    assert lines[0].replace("\\|", "").count("|") == 4


def test_checks_table_renders_every_registered_check_in_order():
    from scripts import deploy

    table = gen_docs.render_checks()
    positions = [table.index(f"`{spec.name}`") for spec in deploy.CHECKS]
    assert positions == sorted(positions), "table order must match run order"
    for spec in deploy.CHECKS:
        assert spec.verifies in table, f"{spec.name}'s description was not rendered"


def test_checks_table_distinguishes_required_from_optional():
    from scripts import deploy

    table = gen_docs.render_checks()
    for line in table.splitlines():
        for spec in deploy.CHECKS:
            if line.startswith(f"| `{spec.name}`"):
                assert ("yes" in line) is spec.required, f"{spec.name} marked wrongly"


def test_checks_table_names_the_keys_that_unlock_the_optional_ones():
    """An operator seeing SKIPPED needs to know which key would unskip it."""
    table = gen_docs.render_checks()
    for key in ("RENDER_API_KEY", "UPTIMEROBOT_API_KEY", "DATABASE_URL"):
        assert key in table


def test_render_checks_is_deterministic():
    assert gen_docs.render_checks() == gen_docs.render_checks()


def test_write_all_creates_exactly_the_four_generated_files(tmp_path):
    written = gen_docs.write_all(tmp_path)
    assert {p.name for p in written} == {
        "config.md", "pricing.md", "checks.md", "sync-env.md"
    }
    for path in written:
        assert path.read_text(encoding="utf-8").startswith(gen_docs.GENERATED_HEADER)


def test_write_all_touches_nothing_outside_guide_reference(tmp_path):
    """The confinement guarantee. A generator that can write anywhere is one
    misplaced argument away from destroying hand-written content."""
    (tmp_path / "guide").mkdir()
    keeper = tmp_path / "guide" / "index.md"
    keeper.write_text("hand-written, must survive\n", encoding="utf-8", newline="\n")

    gen_docs.write_all(tmp_path)

    assert keeper.read_text(encoding="utf-8") == "hand-written, must survive\n"
    produced = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert produced == {
        "guide/index.md",
        "guide/reference/config.md",
        "guide/reference/pricing.md",
        "guide/reference/checks.md",
        "guide/reference/sync-env.md",
    }


def test_write_all_is_idempotent_byte_for_byte(tmp_path):
    """Spec section 8h. The CI drift job compares bytes, so a second run that
    differs at all -- a timestamp, a reordered set -- is a permanent red build."""
    first = {p: p.read_bytes() for p in gen_docs.write_all(tmp_path)}
    second = {p: p.read_bytes() for p in gen_docs.write_all(tmp_path)}
    assert first == second


def test_written_files_use_lf_endings(tmp_path):
    """.gitattributes pins the working tree to LF. A CRLF write on Windows
    would fail the drift check on that operator's machine and nowhere else."""
    for path in gen_docs.write_all(tmp_path):
        assert b"\r\n" not in path.read_bytes()


def test_committed_reference_files_are_up_to_date():
    """The repo's committed output must match what the code generates now --
    the same invariant CI enforces, checked here so a local run catches it."""
    root = Path(__file__).resolve().parent.parent
    for name, render in gen_docs.GENERATED_FILES.items():
        committed = (root / gen_docs.REFERENCE_DIR / name).read_text(encoding="utf-8")
        assert committed == render(), f"{name} is stale -- run scripts.gen_docs"


def test_main_writes_and_reports(tmp_path, capsys):
    assert gen_docs.main(["--root", str(tmp_path)]) == 0
    assert "config.md" in capsys.readouterr().out


def test_every_file_call_in_gen_docs_declares_encoding_and_newline():
    """Spec section 8k / 5a: a missing explicit encoding= on a read or write
    silently falls back to the OS locale encoding (cp1252 on Windows), and a
    missing newline= on a write falls back to CRLF there -- either fails the
    byte-for-byte CI drift check on that operator's machine, since
    .gitattributes pins the working tree to LF. Parsed with ast rather than
    grepped, so a call spanning multiple lines or using different quoting
    still gets caught."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gen_docs))
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
        assert "encoding" in kwargs, f"{name}() at gen_docs.py:{node.lineno} has no encoding="
        if name == "write_text":
            assert "newline" in kwargs, f"{name}() at gen_docs.py:{node.lineno} has no newline="


def test_write_all_output_is_utf8_not_locale_dependent(tmp_path):
    """Spec section 8k / 5a: proves the explicit encoding="utf-8" in
    write_all is load-bearing, not coincidental. The generated checks.md
    contains an em-dash, which cp1252 (the Windows default locale encoding)
    represents as a different single byte than UTF-8's multi-byte form.
    Confirming the file is genuinely UTF-8 on disk -- not some other encoding
    that happens to decode without error -- shows write_all's explicit
    encoding argument, and not luck, is what protects determinism."""
    paths = gen_docs.write_all(tmp_path)
    checks = next(p for p in paths if p.name == "checks.md")
    text = checks.read_text(encoding="utf-8")
    assert "—" in text

    # The bytes actually on disk are UTF-8's multi-byte encoding of the
    # em-dash, not cp1252's single-byte form (0x97).
    raw = checks.read_bytes()
    assert "—".encode("utf-8") in raw
    assert b"\x97" not in raw
    assert "—".encode("utf-8") in raw

    # Round-tripping the bytes on disk through UTF-8 is lossless.
    assert raw.decode("utf-8").encode("utf-8") == raw
