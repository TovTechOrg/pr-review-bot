"""ISSUES.md is 100 KB+ and gets read repeatedly, which makes a whole-file
read one of the most expensive single tool results in a session. A front-
matter index lets a reader `head` the index and then `sed` one entry's range
instead of pulling 100 KB.

An index that drifts is worse than none, because it is trusted. This test is
bidirectional on purpose: every indexed anchor must exist in the body AND
every body entry must appear in the index. Checking one direction alone lets
the index rot silently in the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUES_MD = REPO_ROOT / "ISSUES.md"

# Headings that are structure or templates, not incident entries.
_NON_ENTRY_HEADINGS = {
    "## <short title>",
    "### <short title>",
    "## Parked Issues",
    "## Design Gaps",
}


def _slug(heading_line: str) -> str:
    """GitHub's heading slug. Same rule as tests/test_doc_anchors.py."""
    text = heading_line.lstrip("#").strip().lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return text.replace(" ", "-")


def _split_front_matter(text: str) -> tuple[dict, str]:
    """The YAML front-matter block and the body after it."""
    assert text.startswith("---\n"), (
        "ISSUES.md must open with a '---' YAML front-matter block carrying "
        "read_index. Without it a reader has to pull the whole file to find "
        "one entry."
    )
    end = text.index("\n---\n", 3)
    return yaml.safe_load(text[4:end]), text[end + 5 :]


def _entry_headings(body: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if (line.startswith("## ") or line.startswith("### "))
        and line.strip() not in _NON_ENTRY_HEADINGS
    ]


def _index() -> list[dict]:
    front, _ = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    entries = front.get("read_index")
    assert isinstance(entries, list) and entries, (
        "front-matter has no non-empty 'read_index' list"
    )
    return entries


def test_every_index_entry_has_the_required_keys():
    for entry in _index():
        assert set(entry) >= {"anchor", "title", "parked"}, (
            f"index entry {entry!r} is missing one of anchor/title/parked"
        )
        assert isinstance(entry["parked"], bool), (
            f"index entry {entry['anchor']!r} has a non-boolean 'parked'"
        )


def test_every_indexed_anchor_exists_in_the_body():
    _, body = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    body_slugs = {_slug(h) for h in _entry_headings(body)}
    missing = [e["anchor"] for e in _index() if e["anchor"] not in body_slugs]
    assert not missing, (
        "read_index points at entries that no longer exist in ISSUES.md:\n  "
        + "\n  ".join(missing)
    )


def test_every_body_entry_appears_in_the_index():
    """The direction that actually rots: a new incident gets appended to the
    body and nobody touches the index, so the index quietly describes an old
    file while still looking authoritative."""
    _, body = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    indexed = {e["anchor"] for e in _index()}
    missing = [
        f"{_slug(h)}  ({h.strip()})"
        for h in _entry_headings(body)
        if _slug(h) not in indexed
    ]
    assert not missing, (
        "ISSUES.md entries are missing from read_index -- add one line per "
        "entry when you log it:\n  " + "\n  ".join(missing)
    )


def _parked_range(body: str) -> tuple[int, int]:
    """The '## Parked Issues' section's own extent -- from its heading to the
    next '## ' heading, or EOF if there is none.

    A heading counts as parked only if it falls *inside* this range, not
    merely anywhere after the '## Parked Issues' heading. A structural
    section like '## Design Gaps', or a real top-level incident, can
    legitimately follow '## Parked Issues' in the file without itself being
    parked.
    """
    start = body.index("## Parked Issues")
    after_heading = start + len("## Parked Issues")
    match = re.search(r"^## ", body[after_heading:], re.MULTILINE)
    end = after_heading + match.start() if match else len(body)
    return start, end


def test_parked_range_stops_at_the_next_top_level_heading():
    body = (
        "## Parked Issues\n"
        "### parked entry\n"
        "content\n"
        "## Design Gaps\n"
        "### not parked\n"
        "content\n"
        "## Trailing incident\n"
        "content\n"
    )
    start, end = _parked_range(body)
    assert body[start:end] == "## Parked Issues\n### parked entry\ncontent\n"


def test_parked_flag_matches_where_the_entry_actually_sits():
    """'parked: true' is the whole point of the flag: it is how a reader
    skips forty deferred findings to reach the dozen real incidents."""
    _, body = _split_front_matter(ISSUES_MD.read_text(encoding="utf-8"))
    parked_start, parked_end = _parked_range(body)
    by_anchor = {entry["anchor"]: entry for entry in _index()}
    wrong = []
    for heading in _entry_headings(body):
        entry = by_anchor.get(_slug(heading))
        if entry is None:
            continue  # test_every_body_entry_appears_in_the_index owns this
        pos = body.index(heading)
        actually_parked = parked_start <= pos < parked_end
        if entry["parked"] != actually_parked:
            wrong.append(
                f"{entry['anchor']}: index says parked={entry['parked']}, but "
                f"it sits {'inside' if actually_parked else 'outside'} "
                "the '## Parked Issues' section"
            )
    assert not wrong, "\n".join(wrong)
