"""Whether draft PRs get reviewed: a DB override as cached, no env fallback
(Task 6) -- DB is the sole source of truth. Mirrors tests/test_cooldown_config.py /
tests/test_usage_cap_config.py."""
from __future__ import annotations

import pytest

from review_queue import review_draft_config


@pytest.fixture(autouse=True)
def _clean_cache():
    review_draft_config.reset_override_cache()
    yield
    review_draft_config.reset_override_cache()


def test_no_override_returns_the_not_yet_refreshed_shape():
    assert review_draft_config.effective_review_draft_prs() is None


def test_override_of_true_is_used():
    review_draft_config.set_override_cache(True)
    assert review_draft_config.effective_review_draft_prs() is True


def test_override_of_false_is_used():
    review_draft_config.set_override_cache(False)
    assert review_draft_config.effective_review_draft_prs() is False


def test_reset_override_cache_returns_to_the_not_yet_refreshed_shape():
    review_draft_config.set_override_cache(True)
    review_draft_config.reset_override_cache()
    assert review_draft_config.effective_review_draft_prs() is None
