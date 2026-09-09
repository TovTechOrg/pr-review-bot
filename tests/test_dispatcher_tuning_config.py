"""review_queue/dispatcher_tuning_config.py: no env fallback, per
docs/superpowers/specs/2026-09-08-slotted-config-and-db-delegation-design.md
section 10.4."""
from __future__ import annotations

import pytest

from review_queue import dispatcher_tuning_config as tuning


def _valid_config(**overrides):
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
    config.update(overrides)
    return config


def test_starts_empty_and_reads_back_none_before_any_refresh():
    tuning.reset_override_cache()
    assert tuning.effective_config() == {}


def test_set_override_cache_is_what_effective_config_returns():
    config = _valid_config()
    tuning.set_override_cache(config)
    assert tuning.effective_config() == config
    tuning.reset_override_cache()


def test_problems_is_empty_for_a_valid_config():
    assert tuning.problems(_valid_config()) == []


def test_require_config_returns_the_config_when_valid():
    tuning.set_override_cache(_valid_config())
    assert tuning.require_config() == _valid_config()
    tuning.reset_override_cache()


def test_require_config_raises_on_an_empty_cache():
    tuning.reset_override_cache()
    with pytest.raises(tuning.TuningConfigUnavailable) as excinfo:
        tuning.require_config()
    assert "is not set" in str(excinfo.value)


def test_require_config_raises_on_a_null_column():
    tuning.set_override_cache(_valid_config(dispatcher_backoff_jitter_seconds=None))
    with pytest.raises(tuning.TuningConfigUnavailable) as excinfo:
        tuning.require_config()
    assert "dispatcher_backoff_jitter_seconds is not set" in str(excinfo.value)
    tuning.reset_override_cache()


def test_require_config_rejects_a_zero_notice_sweep_batch_size():
    tuning.set_override_cache(_valid_config(dispatcher_notice_sweep_batch_size=0))
    with pytest.raises(tuning.TuningConfigUnavailable):
        tuning.require_config()
    tuning.reset_override_cache()


def test_require_config_rejects_zero_max_failure_attempts():
    tuning.set_override_cache(_valid_config(dispatcher_max_failure_attempts=0))
    with pytest.raises(tuning.TuningConfigUnavailable):
        tuning.require_config()
    tuning.reset_override_cache()


def test_require_config_rejects_a_base_backoff_above_the_max():
    tuning.set_override_cache(
        _valid_config(
            dispatcher_failure_base_backoff_seconds=500.0,
            dispatcher_failure_max_backoff_seconds=300.0,
        )
    )
    with pytest.raises(tuning.TuningConfigUnavailable) as excinfo:
        tuning.require_config()
    assert "exceeds max backoff" in str(excinfo.value)
    tuning.reset_override_cache()
