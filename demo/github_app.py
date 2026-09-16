"""In-memory stand-in for github_app.py. Reaches no network.

Comments live in a process-local dict so the dashboard can render a
comment_url that looks real without a GitHub round trip.
"""

from __future__ import annotations

import itertools

from demo.content import DEMO_DIFF, DEMO_REPO
from github_app import COMMENT_MARKER, PrDiff  # noqa: F401  (re-exported)

_ids = itertools.count(1001)
_comments: dict[int, "DemoComment"] = {}


class DemoComment:
    """Duck-types PyGithub's IssueComment: .id, .body, .edit()."""

    def __init__(self, comment_id: int, body: str) -> None:
        self.id = comment_id
        self.body = body

    def edit(self, body: str) -> None:
        self.body = body


def reset() -> None:
    _comments.clear()


def fetch_pr_diff(repo: str, pr: int) -> PrDiff:
    return PrDiff(text=DEMO_DIFF, repo_full_name=DEMO_REPO, draft=False)


def upsert_comment(repo: str, pr: int, body: str, comment_id: int | None = None) -> DemoComment:
    if comment_id is not None and comment_id in _comments:
        _comments[comment_id].edit(body)
        return _comments[comment_id]
    new_id = next(_ids)
    _comments[new_id] = DemoComment(new_id, body)
    return _comments[new_id]


def append_review_footnote(repo: str, pr: int, footnote: str,
                           comment_id: int | None = None) -> DemoComment:
    existing = _comments.get(comment_id) if comment_id else None
    base = existing.body if existing else ""
    return upsert_comment(repo, pr, f"{base}\n\n{footnote}", comment_id=comment_id)


def append_schedule_notice(repo: str, pr: int, footnote: str,
                           comment_id: int | None = None) -> DemoComment:
    return append_review_footnote(repo, pr, footnote, comment_id=comment_id)


def clear_schedule_notice(repo: str, pr: int, comment_id: int | None = None) -> DemoComment | None:
    return _comments.get(comment_id) if comment_id else None


def react_eyes_to_pr(repo: str, pr: int) -> None:
    return None


def discover_and_verify_installation_id(expected: int) -> int:
    return 1
