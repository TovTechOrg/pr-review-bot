"""The usage cap actually in force: a DB override as cached, no env fallback
(Task 6) -- DB is the sole source of truth. Mirrors tests/test_cooldown_config.py."""
from __future__ import annotations

from datetime import time

import pytest

from review_queue import usage_cap_config


@pytest.fixture(autouse=True)
def _clean_cache():
    usage_cap_config.reset_override_cache()
    yield
    usage_cap_config.reset_override_cache()


def test_no_override_returns_the_not_yet_refreshed_shape():
    assert usage_cap_config.effective_caps() == (None, None)


def test_full_override_is_used():
    usage_cap_config.set_override_cache(20000, "06:30")
    assert usage_cap_config.effective_caps() == (20000, time(6, 30))


def test_reset_time_accepts_seconds():
    usage_cap_config.set_override_cache(None, "23:59:30")
    assert usage_cap_config.effective_caps()[1] == time(23, 59, 30)


def test_an_unparseable_reset_time_discards_the_whole_pair():
    """All-or-nothing, exactly like cooldown_config: a bad field must never
    pair with a stale override in the other field."""
    usage_cap_config.set_override_cache(20000, "not-a-time")
    assert usage_cap_config.effective_caps() == (None, None)


def test_a_non_positive_cap_normalizes_to_no_cap_but_keeps_the_reset_time():
    """A 0 cap makes the dispatcher's `tokens >= cap` comparison
    unconditionally true -- every ticket deferred forever, and STICKILY, since
    not_before is already a real future timestamp by then. Normalized to
    None (no cap enforced) rather than discarding the whole pair, since the
    reset time is still meaningful for a future cap."""
    usage_cap_config.set_override_cache(0, "06:30")
    assert usage_cap_config.effective_caps() == (None, time(6, 30))


def test_a_none_cap_with_a_real_reset_time_means_cap_intentionally_disabled():
    usage_cap_config.set_override_cache(None, "06:30")
    assert usage_cap_config.effective_caps() == (None, time(6, 30))


def test_effective_caps_returns_a_token_cap_and_a_reset_time():
    usage_cap_config.set_override_cache(20_000, "04:00")
    tokens, reset = usage_cap_config.effective_caps()
    assert tokens == 20_000
    assert reset == time(4, 0)
