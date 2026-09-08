"""The DB-backed cooldown override cache: base/cap/factor overrides, no env
fallback (Task 6) -- DB is the sole source of truth. Mirrors
tests/test_provider_override.py's active-provider-cache tests, but for the
cooldown triple."""
from __future__ import annotations

from review_queue import cooldown_config


def setup_function():
    cooldown_config.reset_override_cache()


def teardown_function():
    cooldown_config.reset_override_cache()


def test_no_override_returns_the_not_yet_refreshed_shape():
    assert cooldown_config.effective_config() == (None, None, None)


def test_full_override_is_used():
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (30.0, 600.0, 1.5)


def test_partial_override_is_discarded_as_a_whole_triple():
    """Unlike the pre-refactor fallback behavior, a partial override (some
    fields None) has nothing to mix with anymore -- the whole triple reads
    back as unavailable."""
    cooldown_config.set_override_cache(30.0, None, None)
    assert cooldown_config.effective_config() == (None, None, None)


def test_invalid_factor_discards_the_whole_triple():
    """A factor < 1 discards the WHOLE override triple, not just the factor --
    a bad factor must not silently pair with a stale overridden base/cap."""
    cooldown_config.set_override_cache(30.0, 600.0, 0.5)
    assert cooldown_config.effective_config() == (None, None, None)


def test_base_above_cap_discards_the_whole_triple():
    cooldown_config.set_override_cache(700.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (None, None, None)


def test_zero_base_is_a_valid_immediate_cooldown():
    """A base of exactly 0 means immediate re-review, not invalid -- only a
    negative base is rejected. See cooldown_config.py's effective_config
    docstring for why this differs from the design doc's literal
    `base <= 0` snippet."""
    cooldown_config.set_override_cache(0.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (0.0, 600.0, 1.5)


def test_negative_base_discards_the_whole_triple():
    cooldown_config.set_override_cache(-5.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (None, None, None)


def test_non_positive_cap_discards_the_whole_triple():
    cooldown_config.set_override_cache(30.0, 0.0, 1.5)
    assert cooldown_config.effective_config() == (None, None, None)


def test_clearing_the_cache_returns_to_the_not_yet_refreshed_shape():
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)
    cooldown_config.reset_override_cache()
    assert cooldown_config.effective_config() == (None, None, None)
