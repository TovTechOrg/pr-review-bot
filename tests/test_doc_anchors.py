"""CLAUDE.md deep-links into docs/conventions/rationale.md by anchor, and
nothing else checks those links. mkdocs --strict covers guide/ only
(mkdocs.yml sets docs_dir: guide), so rationale.md, ISSUES.md and everything
under docs/ sit outside that build entirely -- a renamed or relocated section
silently rots the link pointing at it, and CLAUDE.md is the one file every
session reads.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# `path/to/file.md#anchor`, the way CLAUDE.md spells them (inside backticks).
# The character classes exclude newlines, so a link wrapped across two source
# lines is not matched -- that is a false negative, never a false positive.
_ANCHOR_LINK_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)#([A-Za-z0-9-]+)")


def _slug(heading_line: str) -> str:
    """GitHub's heading slug.

    Verified empirically against every anchor currently in use in both this
    repo and the wizard: lowercase, drop every character outside
    [a-z0-9 -], then spaces to hyphens. Worked example --
    '## Docker image: no `chown -R` (2026-09-07)' becomes
    'docker-image-no-chown--r-2026-09-07' (the colon, backticks and
    parentheses vanish; the space inside 'chown -R' becomes the second
    hyphen of the doubled pair).
    """
    text = heading_line.lstrip("#").strip().lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return text.replace(" ", "-")


def _heading_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]


def _anchor_links() -> list[tuple[str, str]]:
    return _ANCHOR_LINK_RE.findall(CLAUDE_MD.read_text(encoding="utf-8"))


def test_claude_md_actually_contains_anchor_links():
    """A regex that silently stops matching would make every other test here
    pass vacuously. Pin that at least one link is found."""
    assert _anchor_links(), (
        "No `file.md#anchor` links found in CLAUDE.md. Either every deep link "
        "was removed (unlikely) or _ANCHOR_LINK_RE no longer matches how they "
        "are written -- fix the regex, do not delete this test."
    )


def test_every_anchor_link_in_claude_md_resolves():
    broken: list[str] = []
    for rel_path, anchor in _anchor_links():
        target = REPO_ROOT / rel_path
        if not target.exists():
            broken.append(f"{rel_path}#{anchor}: target file does not exist")
            continue
        slugs = {_slug(line) for line in _heading_lines(target)}
        if anchor not in slugs:
            broken.append(
                f"{rel_path}#{anchor}: no heading in {rel_path} slugifies to it"
            )
    assert not broken, (
        "CLAUDE.md has broken anchor links. A relocated or renamed section "
        "leaves the link behind pointing at nothing:\n  " + "\n  ".join(broken)
    )


def test_no_target_file_has_duplicate_heading_slugs():
    """Two headings slugifying identically means GitHub appends '-1' to the
    second, so a link lands on the wrong section while a naive checker still
    sees a match. Catch the ambiguity rather than the symptom."""
    offenders: list[str] = []
    for rel_path in sorted({path for path, _ in _anchor_links()}):
        target = REPO_ROOT / rel_path
        if not target.exists():
            continue
        seen: dict[str, str] = {}
        for line in _heading_lines(target):
            slug = _slug(line)
            if slug in seen:
                offenders.append(
                    f"{rel_path}: {seen[slug]!r} and {line!r} both slugify to {slug!r}"
                )
            else:
                seen[slug] = line
    assert not offenders, (
        "Duplicate heading slugs make anchor links ambiguous:\n  "
        + "\n  ".join(offenders)
    )
