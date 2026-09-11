"""The dashboard's CONFIG_FIELDS registry must agree with the server-side
predicates it mirrors.

The registry is a client-side duplicate of bounds that live in Python. This
file is the mechanism that stops the duplicate drifting -- see
docs/superpowers/specs/2026-09-11-dashboard-typed-config-controls-design.md
section 7.1. It probes each predicate at its boundary rather than reading a
number out of it, because the predicates are opaque lambdas from which no
bound can be extracted mechanically.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from review_queue import (
    cooldown_config,
    dispatcher_tuning_config,
    runtime_config_defaults,
    store,
)

DASHBOARD_HTML = (
    Path(__file__).resolve().parents[1] / "dashboard" / "static" / "dashboard.html"
)

_REGISTRY_RE = re.compile(
    r"// CONFIG_FIELDS_BEGIN.*?const CONFIG_FIELDS = (\[.*?\]);\s*// CONFIG_FIELDS_END",
    re.S,
)

# Every field the config panel edits, by API key. Pinned here so adding a
# field to the form without adding it to the registry fails loudly.
EXPECTED_KEYS = {
    "provider",
    "cooldown_base_seconds",
    "cooldown_factor",
    "cooldown_max_seconds",
    "dispatcher_failure_base_backoff_seconds",
    "dispatcher_failure_max_backoff_seconds",
    "dispatcher_backoff_jitter_seconds",
    "dispatcher_max_failure_attempts",
    "usage_cap_tokens",
    "usage_cap_reset",
    "llm_request_timeout_seconds",
    "dispatcher_default_retry_after_seconds",
    "dispatcher_min_retry_after_seconds",
    "dispatcher_idle_sleep_seconds",
    "dispatcher_notice_sweep_batch_size",
    "dispatcher_max_notice_post_attempts",
    "review_draft_prs",
}

EPS = 1e-9


def load_registry() -> list[dict]:
    """CONFIG_FIELDS, parsed out of the served HTML."""
    match = _REGISTRY_RE.search(DASHBOARD_HTML.read_text(encoding="utf-8"))
    assert match, "CONFIG_FIELDS block not found between its marker comments"
    return json.loads(match.group(1))


def _predicates() -> dict:
    """Column name -> the real server-side predicate for that column."""
    found = {}
    for key, predicate, _description in cooldown_config._BOUNDS:
        found[key] = predicate
    for key, predicate, _description in dispatcher_tuning_config._BOUNDS:
        found[key] = predicate
    # dispatcher_idle_sleep_seconds has NO _BOUNDS entry -- it is read through
    # a separate throttled path, so its rule lives as inline code in
    # dashboard/environment.py:868-875. Transcribed here deliberately; if it
    # ever moves into dispatcher_tuning_config._BOUNDS, delete this line and
    # the loop above will pick it up.
    found["dispatcher_idle_sleep_seconds"] = lambda v: v > 0
    # key_usage_token_cap likewise: usage_cap_config.problems() hand-writes it
    # rather than using a _BOUNDS table, because a None cap is VALID ("cap
    # intentionally disabled"). Only a non-positive number is rejected.
    found["key_usage_token_cap"] = lambda v: v > 0
    return found


def test_registry_is_json_parseable_and_covers_every_field():
    fields = load_registry()
    assert {f["key"] for f in fields} == EXPECTED_KEYS
    assert len(fields) == len(EXPECTED_KEYS), "duplicate key in CONFIG_FIELDS"


def test_every_registry_column_exists_in_the_table():
    declared = {name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS}
    for field in load_registry():
        assert field["column"] in declared, (
            f"{field['key']} names column {field['column']}, "
            "which runtime_config does not have"
        )


@pytest.mark.parametrize(
    "field", [f for f in load_registry() if "min" in f], ids=lambda f: f["key"]
)
def test_registry_min_matches_the_server_predicate(field):
    """Probe the predicate either side of the declared bound.

    An inclusive bound must ACCEPT itself and reject just below it; an
    exclusive bound must REJECT itself and accept just above it.
    """
    predicate = _predicates()[field["column"]]
    minimum = field["min"]
    if field["exclusive"]:
        assert not predicate(minimum), (
            f"{field['key']}: registry says min {minimum} is excluded, "
            "but the server accepts it"
        )
        assert predicate(minimum + EPS)
    else:
        assert predicate(minimum), (
            f"{field['key']}: registry offers min {minimum}, "
            "but the server rejects it"
        )
        assert not predicate(minimum - EPS)


def test_registry_defaults_match_the_declared_column_defaults():
    declared = runtime_config_defaults.declared_defaults()
    for field in load_registry():
        column = field["column"]
        if column in runtime_config_defaults.NO_DEFAULT_BY_DESIGN:
            assert field["default"] is None, (
                f"{field['key']} has no default by design; "
                "the registry must say null"
            )
            continue
        if column not in declared:
            # `provider` is provisioner-owned and has no declared default.
            assert field["default"] is None
            continue
        assert field["default"] == declared[column], (
            f"{field['key']}: registry default {field['default']!r} != "
            f"declared default {declared[column]!r}"
        )


def test_cooldown_preview_pins_the_real_level_ceiling():
    """The preview mirrors store.effective_cooldown, whose exponent is clamped
    at _MAX_COOLDOWN_LEVEL. If that constant moves, the preview silently lies
    about where the sequence ends -- so pin it."""
    assert store._MAX_COOLDOWN_LEVEL == 30
    assert "MAX_COOLDOWN_LEVEL = 30" in DASHBOARD_HTML.read_text(encoding="utf-8")


def test_backoff_preview_pins_the_hardcoded_doubling():
    """dispatcher.compute_backoff doubles -- its factor is NOT the configurable
    cooldown factor. If it ever becomes configurable the preview must gain a
    field, and this assertion is what forces that conversation."""
    import inspect

    from review_queue import dispatcher

    source = inspect.getsource(dispatcher.compute_backoff)
    assert "2 ** (attempts - 1)" in source
    assert "BACKOFF_DOUBLING = 2" in DASHBOARD_HTML.read_text(encoding="utf-8")
