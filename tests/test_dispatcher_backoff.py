"""Pure exponential-backoff math for hard-failure retries (jitter injected)."""
from __future__ import annotations

import pytest

from review_queue import dispatcher, dispatcher_tuning_config


@pytest.fixture(autouse=True)
def _defaults():
    dispatcher_tuning_config.set_override_cache({
        "llm_request_timeout_seconds": 45.0,
        "dispatcher_default_retry_after_seconds": 60.0,
        "dispatcher_failure_base_backoff_seconds": 2.0,
        "dispatcher_failure_max_backoff_seconds": 300.0,
        "dispatcher_max_failure_attempts": 5,
        "dispatcher_max_notice_post_attempts": 3,
        "dispatcher_min_retry_after_seconds": 1.0,
        "dispatcher_backoff_jitter_seconds": 0.0,
        "dispatcher_notice_sweep_batch_size": 20,
    })
    yield
    dispatcher_tuning_config.reset_override_cache()


def test_first_attempt_is_base():
    assert dispatcher.compute_backoff(1, jitter=0.0) == 2.0


def test_backoff_doubles_each_attempt():
    assert dispatcher.compute_backoff(2, jitter=0.0) == 4.0
    assert dispatcher.compute_backoff(3, jitter=0.0) == 8.0
    assert dispatcher.compute_backoff(4, jitter=0.0) == 16.0


def test_backoff_is_capped():
    # 2 * 2**19 would be ~1M; capped at 300.
    assert dispatcher.compute_backoff(20, jitter=0.0) == 300.0


def test_jitter_is_added_on_top():
    assert dispatcher.compute_backoff(1, jitter=5.0) == 7.0


def test_jitter_seam_returns_zero_when_disabled():
    assert dispatcher._jitter() == 0.0


def test_backoff_status_empty_when_nothing_blocked():
    dispatcher.reset_blocked_until()
    assert dispatcher.backoff_status() == {}


def test_backoff_status_reports_blocked_providers():
    from datetime import datetime, timezone

    dispatcher.reset_blocked_until()
    until = datetime(2026, 8, 11, 14, 32, tzinfo=timezone.utc)
    dispatcher._blocked_until["groq"] = until
    assert dispatcher.backoff_status() == {"groq": "2026-08-11T14:32:00+00:00"}
    dispatcher.reset_blocked_until()


def test_compute_backoff_raises_a_named_error_on_an_empty_cache():
    dispatcher_tuning_config.reset_override_cache()
    with pytest.raises(dispatcher_tuning_config.TuningConfigUnavailable):
        dispatcher.compute_backoff(1, jitter=0.0)


def test_jitter_raises_a_named_error_on_an_empty_cache():
    dispatcher_tuning_config.reset_override_cache()
    with pytest.raises(dispatcher_tuning_config.TuningConfigUnavailable):
        dispatcher._jitter()


def test_compute_backoff_uses_an_explicitly_passed_config():
    # Differs from whatever the autouse fixture put in the module cache --
    # proves the explicit `config` argument wins, which is what makes it safe
    # for process_next_due's hard-failure handler to call this from inside
    # its own `except` block without re-reading a cache that may have
    # changed (or emptied) since the guard at the top of the function ran.
    passed = {
        "dispatcher_failure_base_backoff_seconds": 10.0,
        "dispatcher_failure_max_backoff_seconds": 999.0,
    }
    assert dispatcher.compute_backoff(1, jitter=0.0, config=passed) == 10.0
