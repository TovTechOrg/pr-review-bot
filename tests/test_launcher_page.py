"""guide/demo/index.html -- the always-on page the article links to.

Pinned rather than merely present: this file is the FIRST thing a stranger's
browser talks to, it is not exercised by any other test, and its two service
URLs cannot be verified by anything else in this repo.
"""

from __future__ import annotations

import re
from pathlib import Path

LAUNCHER = (
    Path(__file__).resolve().parent.parent / "guide" / "demo" / "index.html"
).read_text(encoding="utf-8")

DEMO_WIZARD_URL = "https://demo-onboarding-wizard.onrender.com"
DEMO_BOT_URL = "https://demo-pr-review-bot.onrender.com"


def test_both_demo_service_urls_are_pinned():
    """Render only suffixes a slug when it collides, so these are stable --
    but a typo here is invisible until a reader hits a dead hostname, which
    is exactly how the sibling repo's DEMO_BOT_URL shipped wrong."""
    assert f'var DEMO_WIZARD_URL = "{DEMO_WIZARD_URL}";' in LAUNCHER
    assert f'var DEMO_BOT_URL = "{DEMO_BOT_URL}";' in LAUNCHER


def test_the_redirect_target_is_never_read_from_the_query_string():
    """?to= selects between two hardcoded constants and nothing else. Feeding
    a query parameter into location.* would turn a page on the org's own
    github.io domain into an open redirect."""
    assert "location.replace(TARGET);" in LAUNCHER
    assert re.search(r"location\.(replace|assign|href)[^\n]*params\.get", LAUNCHER) is None


def test_the_timeout_is_declared_once_and_bounded():
    assert "var TOTAL_MS = 120000;" in LAUNCHER


def test_it_renders_on_a_phone():
    """Mobile is the primary viewport: the audience arrives from LinkedIn."""
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in LAUNCHER


def test_a_no_js_visitor_still_gets_working_links():
    assert "<noscript>" in LAUNCHER
    assert LAUNCHER.count(DEMO_WIZARD_URL) >= 2


def test_nothing_is_loaded_from_a_third_party():
    """Self-contained on purpose: the launcher's whole value is being up when
    the demo is not, so it must not depend on a CDN, a font host, or an
    analytics script."""
    assert "<script src=" not in LAUNCHER
    assert "<link rel=\"stylesheet\"" not in LAUNCHER
