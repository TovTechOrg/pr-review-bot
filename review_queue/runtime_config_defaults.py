"""runtime_config's column names tied to the Settings fields they mirror,
and each column's DECLARED default.

Two facts live here that nothing else in the codebase makes explicit:

1. Three columns do not share their Settings field's name -- the cooldown
   trio is `dispatcher_rereview_cooldown_{seconds,max_seconds,factor}` on
   Settings and `cooldown_{base_seconds,max_seconds,factor}` in the table.
   A `Settings.model_fields[column]` lookup silently KeyErrors on them.
2. `key_usage_reset_time_utc` is a `time` on Settings and TEXT in the
   table. The wire format is `time.isoformat()` (always 3-part, "04:00:00"),
   which is what usage_cap_config parses back with `time.fromisoformat`.

THE ONE RULE, same as scripts/gen_docs.py's: read the Settings CLASS's
`model_fields[...].default`, never the module-level `settings` instance.
These values are written into a database at boot. Reading the instance
would let whatever is in this machine's .env/.env.config decide what a
production boot writes -- and would put credential material one attribute
access away from a code path that logs.
"""
from __future__ import annotations

from datetime import time

from pydantic_core import PydanticUndefined

from config import Settings

COLUMN_TO_SETTING: dict[str, str] = {
    "cooldown_base_seconds": "dispatcher_rereview_cooldown_seconds",
    "cooldown_max_seconds": "dispatcher_rereview_cooldown_max_seconds",
    "cooldown_factor": "dispatcher_rereview_cooldown_factor",
    "key_usage_token_cap": "key_usage_token_cap",
    "key_usage_reset_time_utc": "key_usage_reset_time_utc",
    "review_draft_prs": "review_draft_prs",
    "llm_request_timeout_seconds": "llm_request_timeout_seconds",
    "dispatcher_default_retry_after_seconds": "dispatcher_default_retry_after_seconds",
    "dispatcher_failure_base_backoff_seconds": "dispatcher_failure_base_backoff_seconds",
    "dispatcher_failure_max_backoff_seconds": "dispatcher_failure_max_backoff_seconds",
    "dispatcher_max_failure_attempts": "dispatcher_max_failure_attempts",
    "dispatcher_max_notice_post_attempts": "dispatcher_max_notice_post_attempts",
    "dispatcher_min_retry_after_seconds": "dispatcher_min_retry_after_seconds",
    "dispatcher_backoff_jitter_seconds": "dispatcher_backoff_jitter_seconds",
    "dispatcher_notice_sweep_batch_size": "dispatcher_notice_sweep_batch_size",
    "dispatcher_idle_sleep_seconds": "dispatcher_idle_sleep_seconds",
}

# Columns whose declared default is None -- deliberately left NULL rather
# than backfilled. key_usage_token_cap is the only one: a None cap paired
# with a real reset time is "cap intentionally disabled", a valid
# configured state (see usage_cap_config's docstring), not "unset". A
# COALESCE backfill for it would be a no-op anyway; naming it here makes
# that intentional rather than incidental.
NO_DEFAULT_BY_DESIGN: tuple[str, ...] = ("key_usage_token_cap",)


def _wire_value(value: object) -> float | int | bool | str:
    """A declared default as the column's TEXT/numeric/boolean wire form."""
    if isinstance(value, time):
        return value.isoformat()
    return value  # type: ignore[return-value]


def declared_defaults() -> dict[str, float | int | bool | str]:
    """Each mapped column's declared default, in COLUMN_TO_SETTING order,
    omitting every column whose default is None (see NO_DEFAULT_BY_DESIGN).

    A field declared with `default_factory` instead of `default` reads back
    as PydanticUndefined and is omitted too -- there is none today, and a
    future one needs a deliberate decision here rather than a silent
    `PydanticUndefined` reaching a SQL parameter.
    """
    defaults: dict[str, float | int | bool | str] = {}
    for column, field_name in COLUMN_TO_SETTING.items():
        default = Settings.model_fields[field_name].default
        if default is None or default is PydanticUndefined:
            continue
        defaults[column] = _wire_value(default)
    return defaults
