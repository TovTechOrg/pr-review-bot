"""CLAUDE.md is the only project markdown loaded into every session before
any work begins, so every byte is paid whether or not the content is
relevant to the task at hand. This test pins that cost.

Sections governing credential/secret handling are exempt from the budget:
they are the one thing that must never be trimmed to make a byte count go
green. The separate whole-file cap is what stops "move it into the exempt
section" from becoming the way around the budget.

Overflow belongs in docs/conventions/rationale.md -- a one-line rule here,
the elaboration there -- never in a rule made vaguer to save bytes.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

BUDGET_BYTES = 18_000
TOTAL_CAP_BYTES = 32_000

# A section is budget-exempt only if it governs handling of credentials or
# secrets -- the operator's or a visitor's -- AND trimming it to satisfy a
# byte budget would be a safety regression. Nothing else qualifies. Adding
# an entry here is a deliberate act, not a way to make a red test go green.
EXEMPT_HEADING_PREFIXES = ("## Secret handling",)

_OVERFLOW_ADVICE = (
    "Move the elaboration into docs/conventions/rationale.md and leave a "
    "one-line rule plus a pointer behind. Do NOT trim the secret-handling "
    "section, and do NOT relocate unrelated prose into it to dodge this "
    "budget -- the whole-file cap exists to catch exactly that."
)


def _read() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def _sections(text: str) -> list[tuple[str, str]]:
    """Every '## ' section as (heading_line, full_section_text).

    A section runs from its own heading line to the next '## ' heading, so
    nested '### ' subsections belong to the '## ' section above them --
    which is what lets the wizard nest its visitor-credential rules inside
    ## Secret handling and have them inherit the exemption.

    Lines inside fenced code blocks are skipped: a '## ' written as an
    example inside a fence must not move a section boundary and silently
    change which bytes are exempt.
    """
    lines = text.splitlines(keepends=True)
    in_fence = False
    starts: list[int] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            starts.append(i)
    out: list[tuple[str, str]] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        out.append((lines[start].rstrip("\n"), "".join(lines[start:end])))
    return out


def _exempt_bytes(text: str) -> int:
    return sum(
        len(body.encode("utf-8"))
        for heading, body in _sections(text)
        if heading.startswith(EXEMPT_HEADING_PREFIXES)
    )


def test_exempt_headings_are_present():
    """Deleting or renaming an exempt heading would silently enlarge the
    budgeted region -- and the obvious way to "fix" a red budget is to make
    the exempt section bigger, not smaller. Fail loudly instead."""
    headings = [heading for heading, _ in _sections(_read())]
    for prefix in EXEMPT_HEADING_PREFIXES:
        assert any(heading.startswith(prefix) for heading in headings), (
            f"CLAUDE.md has no {prefix!r} section. The budget exempts that "
            "section by heading, so removing or renaming it changes what is "
            "measured. Restore the heading."
        )


def test_budgeted_bytes_are_within_budget():
    text = _read()
    budgeted = len(text.encode("utf-8")) - _exempt_bytes(text)
    assert budgeted <= BUDGET_BYTES, (
        f"CLAUDE.md's non-exempt content is {budgeted} bytes, over the "
        f"{BUDGET_BYTES}-byte budget by {budgeted - BUDGET_BYTES}. "
        + _OVERFLOW_ADVICE
    )


def test_whole_file_is_within_the_total_cap():
    size = len(_read().encode("utf-8"))
    assert size <= TOTAL_CAP_BYTES, (
        f"CLAUDE.md is {size} bytes, over the {TOTAL_CAP_BYTES}-byte "
        f"whole-file cap by {size - TOTAL_CAP_BYTES}. " + _OVERFLOW_ADVICE
    )


def test_claude_md_has_no_crlf_line_endings():
    """CRLF endings and a BOM change the byte count without changing a word,
    making the budget flap for a reason no reader can see in a diff."""
    raw = CLAUDE_MD.read_bytes()
    assert b"\r\n" not in raw, (
        "CLAUDE.md has CRLF line endings. Every line silently costs one "
        "extra byte against the budget. Convert to LF."
    )
    assert not raw.startswith(b"\xef\xbb\xbf"), (
        "CLAUDE.md starts with a UTF-8 BOM, which costs three bytes and "
        "breaks the '## Secret handling' prefix match on the first heading."
    )


def test_section_split_ignores_fenced_code_blocks():
    """A '## ' inside a fence is an example, not a section boundary. If it
    were treated as one, the exempt region would end early and unrelated
    prose would silently become exempt."""
    text = (
        "# Title\n"
        "## Secret handling\n"
        "real secret rules\n"
        "```markdown\n"
        "## Not a real heading\n"
        "```\n"
        "still inside secret handling\n"
        "## Conventions\n"
        "budgeted prose\n"
    )
    headings = [heading for heading, _ in _sections(text)]
    assert headings == ["## Secret handling", "## Conventions"]
    exempt = next(body for heading, body in _sections(text) if "Secret" in heading)
    assert "still inside secret handling" in exempt
