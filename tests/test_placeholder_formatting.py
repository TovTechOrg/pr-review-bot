from __future__ import annotations

from datetime import datetime, timezone

from formatting import format_placeholder
from github_app import COMMENT_MARKER

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_short_wait_is_rate_limit_wording_and_has_marker():
    body = format_placeholder(pr_number=42, retry_after=30.0, now=NOW)
    assert COMMENT_MARKER in body
    assert "## Automated Code Review\n" in body
    assert "PR #" not in body
    assert "🤖" not in body
    assert "rate limit" in body.lower()


def test_long_wait_is_daily_quota_wording_with_eta_and_marker():
    # 6 hours -> 18:00 UTC
    body = format_placeholder(pr_number=42, retry_after=6 * 3600, now=NOW)
    assert COMMENT_MARKER in body
    assert "daily" in body.lower()
    assert "18:00 UTC" in body


def test_format_failure_has_marker_pr_and_attempts_no_error_text():
    from formatting import format_failure

    body = format_failure(pr_number=42, attempts=5)
    assert COMMENT_MARKER in body
    assert "## Automated Code Review\n" in body
    assert "PR #" not in body
    assert "5" in body                       # attempt count surfaced
    assert "traceback" not in body.lower()   # no raw error/exception text


def test_format_failure_singular_grammar():
    from formatting import format_failure

    body = format_failure(pr_number=1, attempts=1)
    assert "1 attempt" in body
    assert "1 attempts" not in body


def test_format_failure_footnote_submarkers_and_grammar():
    from formatting import format_failure_footnote
    from github_app import FAIL_NOTE_END, FAIL_NOTE_START

    body = format_failure_footnote(attempts=3)
    assert FAIL_NOTE_START in body and FAIL_NOTE_END in body
    assert "3 attempts" in body
    assert "traceback" not in body.lower()  # no raw error text

    single = format_failure_footnote(attempts=1)
    assert "1 attempt" in single and "1 attempts" not in single


def test_usage_cap_placeholder_is_distinct_from_a_provider_rate_limit():
    """The bot's own cap must not read as the provider's problem -- an
    operator debugging a stalled review needs to know which limit hit."""
    body = format_placeholder(pr_number=42, retry_after=6 * 3600, now=NOW, reason="usage_cap")
    assert COMMENT_MARKER in body
    assert "## Automated Code Review\n" in body
    assert "usage limit" in body.lower()
    assert "not a provider rate limit" in body.lower()
    assert "18:00 UTC" in body                      # ETA still computed from now+retry_after


def test_usage_cap_placeholder_wording_ignores_the_wait_magnitude():
    """Unlike the provider branch, the usage-cap wording does not switch on
    short-vs-long waits -- the cause is the same either way."""
    short = format_placeholder(pr_number=42, retry_after=30.0, now=NOW, reason="usage_cap")
    assert "usage limit" in short.lower()
    assert "queued behind rate limit" not in short.lower()
    assert "12:00 UTC" in short                     # now + 30s still rounds to 12:00


def test_placeholder_default_reason_is_byte_identical_to_explicit_provider():
    """Every existing call site passes no `reason` -- it must render
    byte-identically to passing reason="provider" explicitly."""
    for retry_after in (30.0, 6 * 3600):
        assert format_placeholder(42, retry_after, NOW) == format_placeholder(
            42, retry_after, NOW, reason="provider"
        )
    assert "Queued behind rate limit" in format_placeholder(42, 30.0, NOW)
    assert "Daily model quota reached" in format_placeholder(42, 6 * 3600, NOW)
