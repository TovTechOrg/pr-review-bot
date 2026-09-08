"""review_queue/dispatcher_tuning_config.py: no env fallback, per
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 10.4."""
from __future__ import annotations

from review_queue import dispatcher_tuning_config as tuning


def test_starts_empty_and_reads_back_none_before_any_refresh():
    tuning.reset_override_cache()
    assert tuning.effective_config() == {}


def test_set_override_cache_is_what_effective_config_returns():
    config = {
        "llm_request_timeout_seconds": 45.0,
        "dispatcher_default_retry_after_seconds": 60.0,
        "dispatcher_failure_base_backoff_seconds": 2.0,
        "dispatcher_failure_max_backoff_seconds": 300.0,
        "dispatcher_max_failure_attempts": 5,
        "dispatcher_max_notice_post_attempts": 3,
        "dispatcher_min_retry_after_seconds": 1.0,
        "dispatcher_backoff_jitter_seconds": 0.0,
        "dispatcher_notice_sweep_batch_size": 20,
    }
    tuning.set_override_cache(config)
    assert tuning.effective_config() == config
    tuning.reset_override_cache()
