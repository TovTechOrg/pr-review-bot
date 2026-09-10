"""Compare a consumer's vendored provisioning contract against this repo's own.

    uv run python -m scripts.check_consumer_contract --consumer-root <dir>

ADVISORY ONLY (spec section 6.2). This module's output must never gate a
push, a merge, or a deploy in THIS repository -- the workflow that calls it
(.github/workflows/consumer-contract-lag.yml) is deliberately not attached to
any push/pull_request trigger. A red run here means the consumer
(TovTechOrg/onboarding-wizard) has not yet vendored a contract change this
repo already made -- an expected, transient state, not a defect in this repo.

Three verdicts, not two, because "the consumer disagrees with us" and "we
could not even check" must never look the same in a log:

- IN_SYNC     -- the vendored copy is byte-identical to this repo's contract.
- LAGGING     -- the comparison ran; the consumer has not caught up. Expected
                 and transient. Also the verdict for a consumer that has not
                 vendored a contract at all yet (spec stage 3 unlanded) --
                 that is exactly what "lagging" means, not a tooling failure.
- UNCHECKABLE -- the comparison could not be performed at all: the sibling
                 checkout failed (private/renamed/deleted repo), this
                 repo's OWN committed contract is stale, or the bot's
                 contract text itself failed to parse. A genuine problem
                 with this tooling, not with the consumer.

The consumer's `.ci/pr-review-bot-ref` pin is read and reported for CONTEXT
ONLY and never changes the verdict. An old pin is not lag: the wizard can be
pinned forty commits behind `main` and still be perfectly in sync if none of
those commits touched the contract. Only a genuinely differing contract is
lag. Getting this backwards would make the job permanently red.

The contract carries env-var names, placement labels, SQL types and
non-secret operational defaults only (spec section 5.3) -- never a
credential value -- which is why this module may print a full field-level
diff into a public workflow log. Nothing else may reach that log: this
module never imports config.settings (see gen_contract.py's "THE ONE RULE"),
never shells out with a value it did not first validate, and never runs
`git` with untrusted input as part of a shell string.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from scripts import gen_contract

IN_SYNC = "IN_SYNC"
LAGGING = "LAGGING"
UNCHECKABLE = "UNCHECKABLE"

EXIT_CODE: dict[str, int] = {IN_SYNC: 0, LAGGING: 1, UNCHECKABLE: 2}

CONSUMER_REPO = "TovTechOrg/onboarding-wizard"
CONSUMER_PIN_PATH = ".ci/pr-review-bot-ref"
# The same relative path in both repos by design (spec section 5.5).
CONSUMER_CONTRACT_PATH = gen_contract.CONTRACT_PATH

_PIN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Detail lines are capped so a contract_version bump (which can touch every
# field) produces a readable summary instead of hundreds of lines.
_MAX_DETAIL_LINES = 50


@dataclass(frozen=True)
class Report:
    verdict: str
    headline: str
    details: list[str] = field(default_factory=list)
    bot_version: int | None = None
    consumer_version: int | None = None
    pin: str | None = None
    pin_context: str | None = None


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    """Every leaf of a contract document, addressed by a dotted path.

    A list of dicts each carrying a "column" key is keyed BY COLUMN NAME, not
    by index: runtime_config.bot_backfilled is exactly such a list, and
    inserting one column into it would otherwise shift every later entry and
    report one real addition as a dozen spurious changes. Everything else --
    including lists of plain strings such as provisioner_required, whose
    ORDER is load-bearing and asserted by tests/test_provisioning_contract.py
    -- is a leaf compared as a whole value.
    """
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, value in node.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(value, child_prefix))
        return out
    if (
        isinstance(node, list)
        and node
        and all(isinstance(item, dict) and "column" in item for item in node)
    ):
        out = {}
        for item in node:
            rest = {k: v for k, v in item.items() if k != "column"}
            out.update(_flatten(rest, f"{prefix}[{item['column']}]"))
        return out
    return {prefix: node}


def _render_value(value: object) -> str:
    return json.dumps(value)


def differences(bot: dict, consumer: dict) -> list[str]:
    """Every dotted-path difference between two contract documents.

    Three groups, each sorted by path for a deterministic report: paths only
    the bot publishes, paths only the consumer's copy carries (stale -- this
    repo no longer publishes them), and shared paths whose values differ.
    """
    bot_flat = _flatten(bot)
    consumer_flat = _flatten(consumer)
    bot_only = sorted(set(bot_flat) - set(consumer_flat))
    consumer_only = sorted(set(consumer_flat) - set(bot_flat))
    shared_changed = sorted(
        path
        for path in set(bot_flat) & set(consumer_flat)
        if bot_flat[path] != consumer_flat[path]
    )

    lines: list[str] = []
    for path in bot_only:
        lines.append(
            f"`{path}` is missing from the consumer's copy "
            f"(this repo publishes {_render_value(bot_flat[path])})"
        )
    for path in consumer_only:
        lines.append(
            f"`{path}` is stale in the consumer's copy -- this repo no longer publishes it "
            f"(consumer still has {_render_value(consumer_flat[path])})"
        )
    for path in shared_changed:
        lines.append(
            f"`{path}`: consumer has {_render_value(consumer_flat[path])}, "
            f"this repo publishes {_render_value(bot_flat[path])}"
        )
    return lines


def compare(
    bot_text: str,
    consumer_text: str | None,
    *,
    pin: str | None = None,
    pin_context: str | None = None,
) -> Report:
    """Compare this repo's contract text against a consumer's vendored copy.

    consumer_text is None when the consumer has no vendored contract at all
    -- reported as LAGGING, not an error, so stage 4 stands alone without
    stage 3 having landed in the other repository.
    """
    try:
        bot = json.loads(bot_text)
    except json.JSONDecodeError as exc:
        return Report(
            verdict=UNCHECKABLE,
            headline=f"this repository's own generated contract is not valid JSON: {exc}",
            pin=pin,
            pin_context=pin_context,
        )
    bot_version = bot.get("contract_version") if isinstance(bot, dict) else None

    if consumer_text is None:
        return Report(
            verdict=LAGGING,
            headline=f"{CONSUMER_REPO} has not vendored the contract yet",
            bot_version=bot_version,
            pin=pin,
            pin_context=pin_context,
        )

    try:
        consumer = json.loads(consumer_text)
    except json.JSONDecodeError as exc:
        return Report(
            verdict=LAGGING,
            headline=f"{CONSUMER_REPO}'s vendored copy is not valid JSON: {exc}",
            bot_version=bot_version,
            pin=pin,
            pin_context=pin_context,
        )
    consumer_version = consumer.get("contract_version") if isinstance(consumer, dict) else None

    if bot_text == consumer_text:
        return Report(
            verdict=IN_SYNC,
            headline=f"{CONSUMER_REPO} is in sync",
            bot_version=bot_version,
            consumer_version=consumer_version,
            pin=pin,
            pin_context=pin_context,
        )

    if bot_version != consumer_version:
        headline = (
            f"{CONSUMER_REPO} is on contract_version {consumer_version!r}, "
            f"this repo is on {bot_version!r} -- a shape change, not just a value change"
        )
        details = differences(bot, consumer)
        details.insert(
            0, "field-level differences below may be noise across a contract_version change:"
        )
    else:
        details = differences(bot, consumer)
        if details:
            headline = (
                f"{CONSUMER_REPO}'s vendored contract is {len(details)} field(s) "
                "behind this repository's"
            )
        else:
            headline = (
                f"{CONSUMER_REPO}'s vendored contract differs byte-for-byte "
                "with no field-level difference (formatting/ordering drift) -- "
                "still real lag, since the consumer's own byte-identity parity "
                "test will fail on it"
            )

    if len(details) > _MAX_DETAIL_LINES:
        details = details[:_MAX_DETAIL_LINES] + [f"... and {len(details) - _MAX_DETAIL_LINES} more"]

    return Report(
        verdict=LAGGING,
        headline=headline,
        details=details,
        bot_version=bot_version,
        consumer_version=consumer_version,
        pin=pin,
        pin_context=pin_context,
    )


def render_report(report: Report) -> str:
    lines = [
        f"## Consumer contract lag -- {CONSUMER_REPO} @ main",
        "",
        f"**{report.verdict}** -- {report.headline}",
        "",
    ]

    rows = []
    if report.bot_version is not None:
        rows.append(("This repo's contract_version", str(report.bot_version)))
    if report.consumer_version is not None:
        rows.append(("Consumer's contract_version", str(report.consumer_version)))
    if report.pin is not None:
        pin_line = report.pin
        if report.pin_context:
            pin_line += f" -- {report.pin_context}"
        rows.append((f"Consumer's pin (`{CONSUMER_PIN_PATH}`)", pin_line))
    if rows:
        lines.append("| | |")
        lines.append("|---|---|")
        for label, value in rows:
            lines.append(f"| {label} | {value} |")
        lines.append("")

    if report.details:
        lines.append("### What the consumer has not picked up")
        lines.extend(f"- {detail}" for detail in report.details)
        lines.append("")

    if report.verdict == LAGGING:
        lines.append("### What closes this")
        lines.append(f"In the **consumer** repository ({CONSUMER_REPO}):")
        lines.append("")
        lines.append("    uv run python scripts/update_bot_contract.py")
        lines.append("")
        lines.append(
            f"then commit `contracts/provisioning.json` and `{CONSUMER_PIN_PATH}` together."
        )
        lines.append("")

    lines.append("---")
    lines.append(
        "_Advisory only. This job is not attached to any push or pull-request trigger "
        "and gates nothing in this repository -- see spec section 6.2._"
    )
    return "\n".join(lines) + "\n"


def load_consumer(root: Path) -> tuple[str | None, str | None, str | None]:
    """Read the consumer's vendored contract and pin out of a checkout tree.

    Returns (contract_text, pin_sha, unreachable_reason).

    `root` not existing at all means the checkout step itself failed --
    private/renamed/deleted repo -- and is reported as unreachable
    (UNCHECKABLE). `root` existing but with no contract file at that path
    means the consumer simply has not vendored one yet -- LAGGING, not an
    error (spec stage 3 may not have landed). Keeping those two apart is the
    entire reason this takes a directory rather than a file path.
    """
    if not root.is_dir():
        return None, None, f"consumer checkout directory not found: {root}"

    contract_path = root / CONSUMER_CONTRACT_PATH
    contract_text = (
        contract_path.read_text(encoding="utf-8") if contract_path.is_file() else None
    )

    pin_path = root / CONSUMER_PIN_PATH
    pin_sha = None
    if pin_path.is_file():
        for line in pin_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                pin_sha = stripped
                break

    return contract_text, pin_sha, None


def pin_context(sha: str, repo_root: Path) -> str | None:
    """A human-readable sentence about where `sha` sits relative to HEAD.

    The pin is REPORT CONTEXT ONLY -- see the module docstring. This never
    RAISES on a bad format or a git failure (the caller in _build_report()
    also wraps this call in its own try/except as a second guarantee, since
    the pin must never be able to override an otherwise-valid verdict): a
    malformed sha or an unreachable commit still return an explanatory
    STRING rather than None, so the report can say what went wrong; None is
    returned only when the sha itself resolves but a later `git` call
    (rev-list/show) fails for an unrelated reason.

    The sha is validated against spec section 5.5's exact pinned format
    (40 lowercase hex characters) BEFORE it reaches subprocess, with a fixed
    argv list and shell=False throughout, so a corrupt or hostile pin file
    can never become a shell command.
    """
    if not _PIN_SHA_RE.match(sha):
        return "the pin is not a 40-character hex commit sha -- cannot resolve its context"

    def _git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    if _git("merge-base", "--is-ancestor", sha, "HEAD") is None:
        return "the consumer's pin names a commit not reachable from this repository's main"

    commit_count = _git("rev-list", "--count", f"{sha}..HEAD")
    commit_date = _git("show", "-s", "--format=%cs", sha)
    if commit_count is None or commit_date is None:
        return None
    return f"{commit_count} commits behind main, {commit_date}"


def _annotation(verdict: str, headline: str) -> str:
    level = {IN_SYNC: "notice", LAGGING: "warning", UNCHECKABLE: "error"}[verdict]
    return f"::{level}::{CONSUMER_REPO} contract check -- {verdict}: {headline}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare TovTechOrg/onboarding-wizard's vendored contract "
        "against this repo's own"
    )
    parser.add_argument(
        "--consumer-root", required=True, help="checkout directory for the consumer repository"
    )
    parser.add_argument(
        "--consumer-checkout-outcome",
        default="success",
        help=(
            "the outcome of the workflow step that checked out --consumer-root "
            "(e.g. GitHub Actions' steps.<id>.outcome). A non-'success' value is "
            "UNCHECKABLE regardless of what --consumer-root contains -- actions/checkout "
            "creates its target directory (and runs `git init` in it) before it can fail "
            "on a private/renamed/deleted repository, so an unreachable sibling is NOT "
            "reliably distinguishable from 'not vendored yet' by inspecting the directory "
            "alone. Left at its default when running this script outside that workflow."
        ),
    )
    parser.add_argument(
        "--bot-repo-root",
        default=".",
        help="this repository's own checkout, used to resolve the pin's commit context",
    )
    parser.add_argument(
        "--bot-contract",
        default=None,
        help="override this repo's contract text (default: regenerated in-process)",
    )
    parser.add_argument(
        "--committed-contract",
        default=gen_contract.CONTRACT_PATH,
        help="this repo's committed contract file, cross-checked for staleness",
    )
    parser.add_argument(
        "--summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="path to append the markdown report to (default: $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument(
        "--never-fail",
        action="store_true",
        help="always exit 0 regardless of verdict (Decision 1's warn-only alternative)",
    )
    args = parser.parse_args(argv)

    try:
        report = _build_report(args)
    except Exception as exc:  # noqa: BLE001 -- this script's entire job is to always report
        report = Report(
            verdict=UNCHECKABLE,
            headline=f"the check itself raised an unexpected error: {exc!r}",
        )

    rendered = render_report(report)
    print(rendered)
    print(_annotation(report.verdict, report.headline))

    if args.summary:
        try:
            with open(args.summary, "a", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as exc:
            # The report was already printed to stdout above; a failure to
            # ALSO append it to the step summary must not crash a script
            # whose entire job is to never traceback out.
            print(f"::warning::could not write the report to {args.summary}: {exc!r}")

    if args.never_fail:
        return 0
    return EXIT_CODE[report.verdict]


def _build_report(args: argparse.Namespace) -> Report:
    committed_path = Path(args.committed_contract)
    committed_text = (
        committed_path.read_text(encoding="utf-8") if committed_path.is_file() else None
    )
    bot_text = args.bot_contract
    if bot_text is None:
        bot_text = gen_contract.render()
        if committed_text is None:
            return Report(
                verdict=UNCHECKABLE,
                headline=f"this repository's committed {args.committed_contract} is missing",
            )
        if committed_text != bot_text:
            return Report(
                verdict=UNCHECKABLE,
                headline=(
                    f"this repository's committed {args.committed_contract} is stale -- "
                    "run scripts.gen_contract and check ci.yml's docs job"
                ),
            )

    if args.consumer_checkout_outcome != "success":
        return Report(
            verdict=UNCHECKABLE,
            headline=(
                f"the consumer checkout step did not succeed (outcome="
                f"{args.consumer_checkout_outcome!r}) -- the sibling repository may be "
                "private, renamed, or deleted; see spec section 6.2"
            ),
        )

    consumer_root = Path(args.consumer_root)
    contract_text, pin_sha, unreachable_reason = load_consumer(consumer_root)
    if unreachable_reason is not None:
        return Report(verdict=UNCHECKABLE, headline=unreachable_reason)

    # The pin is report CONTEXT ONLY (see module docstring) -- a failure resolving
    # its commit context (e.g. an undecodable `git show` message) must never
    # override an otherwise-valid comparison result with UNCHECKABLE.
    context = None
    if pin_sha:
        try:
            context = pin_context(pin_sha, Path(args.bot_repo_root))
        except Exception:  # noqa: BLE001 -- pin context is never allowed to affect the verdict
            context = None
    return compare(bot_text, contract_text, pin=pin_sha, pin_context=context)


if __name__ == "__main__":
    raise SystemExit(main())
