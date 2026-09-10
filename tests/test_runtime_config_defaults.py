"""COLUMN_TO_SETTING is the one place runtime_config's column names are
tied to Settings' field names -- they differ for the cooldown trio, and
key_usage_reset_time_utc's declared default is a `time` while its column
is TEXT. Both facts used to live only inside deploy.py::sync_config_db()."""
from datetime import time

from config import Settings
from review_queue import runtime_config_defaults as rcd
from review_queue.store import RUNTIME_CONFIG_COLUMNS


def test_every_mapped_column_exists_in_the_schema():
    declared = {name for name, _sql_type in RUNTIME_CONFIG_COLUMNS}
    unknown = set(rcd.COLUMN_TO_SETTING) - declared
    assert not unknown, f"COLUMN_TO_SETTING names no such column: {sorted(unknown)}"


def test_every_mapped_setting_exists_on_settings():
    unknown = set(rcd.COLUMN_TO_SETTING.values()) - set(Settings.model_fields)
    assert not unknown, f"COLUMN_TO_SETTING names no such Settings field: {sorted(unknown)}"


def test_cooldown_columns_map_to_their_differently_named_settings():
    # The rename that a naive Settings.model_fields[column] lookup gets wrong.
    assert rcd.COLUMN_TO_SETTING["cooldown_base_seconds"] == (
        "dispatcher_rereview_cooldown_seconds"
    )
    assert rcd.COLUMN_TO_SETTING["cooldown_max_seconds"] == (
        "dispatcher_rereview_cooldown_max_seconds"
    )
    assert rcd.COLUMN_TO_SETTING["cooldown_factor"] == "dispatcher_rereview_cooldown_factor"


def test_declared_defaults_serializes_time_to_isoformat():
    # The column is TEXT; usage_cap_config parses it with time.fromisoformat.
    assert Settings.model_fields["key_usage_reset_time_utc"].default == time(4, 0)
    assert rcd.declared_defaults()["key_usage_reset_time_utc"] == "04:00:00"


def test_declared_defaults_omits_none_defaulted_columns():
    assert "key_usage_token_cap" not in rcd.declared_defaults()
    assert "key_usage_token_cap" in rcd.NO_DEFAULT_BY_DESIGN


def test_declared_defaults_reads_class_defaults_not_the_instance(monkeypatch):
    # A stray env var must not change what a boot writes into the database.
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "999.0")
    assert rcd.declared_defaults()["llm_request_timeout_seconds"] == 45.0


def test_declared_defaults_covers_every_mapped_column_except_none_defaults():
    expected = set(rcd.COLUMN_TO_SETTING) - set(rcd.NO_DEFAULT_BY_DESIGN)
    assert set(rcd.declared_defaults()) == expected
