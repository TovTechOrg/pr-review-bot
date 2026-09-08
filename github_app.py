"""GitHub App authentication, PR diff fetching, and comment upsert.

Narrow responsibility: authenticate as the GitHub App installation (JWT ->
short-lived installation token via PyGithub's ``Auth`` API), fetch a PR's
unified diff, and find-or-create-or-edit the bot's single PR comment.

Knows nothing about LLMs, orchestration, or diff annotation/truncation —
those belong to ``orchestrator.py`` / ``diff_utils.py``.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from github import Auth, Github, GithubException
from github.IssueComment import IssueComment

from config import settings

# Hidden marker used to find the bot's own comment across re-reviews so a
# `synchronize` event edits it in place instead of spamming a new one.
COMMENT_MARKER = "<!-- ai-code-review-bot -->"

# Sub-marker delimiting an optional failure footnote appended below a preserved
# good review. Idempotent: a new footnote replaces any prior block, and a later
# successful review's full-body overwrite (via upsert_comment) removes it.
FAIL_NOTE_START = "<!-- ai-review-fail-note -->"
FAIL_NOTE_END = "<!-- /ai-review-fail-note -->"

# Sub-marker delimiting the self-cleaning "re-review scheduled" notice shown
# while a cooldown/rate-limit wait is pending. NOT mutually exclusive with
# FAIL_NOTE_* by ticket-state construction alone -- a ticket that hits the
# failure ceiling (fail note posted) and is then pushed can briefly carry
# both footnote kinds on GitHub at once. What actually guarantees only one
# is ever visible is _strip_existing_footnote recognizing both marker pairs:
# whichever footnote-writing function runs next cleans up the other kind.
SCHEDULE_NOTE_START = "<!-- ai-review-schedule-note -->"
SCHEDULE_NOTE_END = "<!-- /ai-review-schedule-note -->"


def _is_bot_comment(comment: IssueComment) -> bool:
    """True if authored by a GitHub App bot (not a human), so a human quoting
    the marker is never mistaken for the bot's own comment."""
    return getattr(comment.user, "type", None) == "Bot"


def _find_bot_comment(repo, pr, comment_id: int | None) -> IssueComment | None:
    """Locate the bot's own comment: by stored id first (trusted — we created it),
    else an author-filtered marker scan (bot-authored AND our marker). Returns
    None if neither finds one, so the caller creates a fresh marker comment."""
    if comment_id is not None:
        try:
            headers, data = repo.requester.requestJsonAndCheck(
                "GET", f"/repos/{repo.full_name}/issues/comments/{comment_id}"
            )
            return IssueComment(repo.requester, headers, data, completed=True)
        except GithubException:
            pass  # deleted/unknown id -> fall back to the scan
    for comment in pr.get_issue_comments():
        if _is_bot_comment(comment) and COMMENT_MARKER in comment.body:
            return comment
    return None


_FOOTNOTE_MARKERS = (
    (FAIL_NOTE_START, FAIL_NOTE_END),
    (SCHEDULE_NOTE_START, SCHEDULE_NOTE_END),
)


def _strip_existing_footnote(body: str) -> str:
    """Strip whichever known footnote block (failure note or schedule note) is
    present as a well-formed TRAILING block, if any.

    Deliberately NOT a regex-from-first-marker-to-next-marker scan: a
    specialist's finding text could plausibly quote a literal marker string
    (this very file contains both), which would make a "first START to next
    END" match span from that stray marker all the way to the real trailing
    footnote's END, deleting genuine review content in between. Instead: for
    each known marker pair, find the LAST occurrence of its START and only
    treat it as a real footnote to strip if the body actually ends with its
    END -- any earlier/unmatched occurrence is left alone as incidental
    review text. Trying both pairs means whichever footnote-writing function
    runs next cleans up a stale leftover of the OTHER kind too, so the two
    kinds can never both be visible at once even if an earlier cleanup step
    failed.
    """
    stripped = body.rstrip()
    for start, end in _FOOTNOTE_MARKERS:
        idx = stripped.rfind(start)
        if idx != -1 and stripped.endswith(end):
            return stripped[:idx].rstrip()
    return stripped


def _decode_private_key(private_key_b64: str) -> str:
    try:
        return base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "GITHUB_APP_PRIVATE_KEY is not valid base64 -- encode the PEM with: "
            "uv run python -m scripts.encode_credential github-app-private-key.pem"
        ) from exc


def _read_private_key() -> str:
    """Decode the base64-encoded App private key. Never logged."""
    return _decode_private_key(settings.github_app_private_key)


def get_installation_auth(installation_id: int | None = None) -> Auth.AppInstallationAuth:
    """Build the installation-level auth object (JWT -> installation token).

    Uses ``Auth.AppAuth`` (signs a JWT with the App's private key) wrapped in
    ``Auth.AppInstallationAuth``, which PyGithub transparently exchanges for
    a short-lived installation access token and refreshes as needed. This is
    NOT a JWT-only auth and NOT a personal-access-token auth.

    `installation_id`, when given, overrides `settings.github_app_installation_id`
    -- used by callers (e.g. list_installation_repos) that just discovered a
    fresh id and must not depend on that setting having been assigned yet.
    """
    app_auth = Auth.AppAuth(settings.github_app_id, _read_private_key())
    resolved_id = (
        installation_id if installation_id is not None else settings.github_app_installation_id
    )
    return app_auth.get_installation_auth(resolved_id)


def get_installation_client(installation_id: int | None = None) -> Github:
    """Build a ``Github`` client authenticated as the App installation.

    `installation_id` overrides `settings.github_app_installation_id` when
    given -- see get_installation_auth.
    """
    return Github(auth=get_installation_auth(installation_id))


def _app_jwt_client() -> Github:
    """Client authenticated as the App itself (JWT), for App-level endpoints."""
    return Github(auth=Auth.AppAuth(settings.github_app_id, _read_private_key()))


def _app_jwt_client_for(app_id: int, private_key_b64: str) -> Github:
    """Client authenticated as an ARBITRARY App identity (JWT) -- for
    validating a candidate App ID + private key before it's ever written to
    Settings/Render. Used only by the dashboard's guided credential flow;
    every other caller in this file keeps using _app_jwt_client(), which
    reads the currently-configured identity."""
    return Github(auth=Auth.AppAuth(app_id, _decode_private_key(private_key_b64)))


class AppNotInstalledError(RuntimeError):
    """The App has no installation covering what a caller asked about: either
    a specific repo (discover_installation_id, GitHub returned 404) or the
    App as a whole (discover_installation_id_for_app, an empty
    GET /app/installations response -- no 404 involved).

    Subclasses RuntimeError so existing callers and tests that catch
    RuntimeError keep working; the distinct type exists so a caller can branch
    on "not installed" without matching on message text.
    """


def discover_installation_id(repo_full_name: str) -> int:
    """Return the installation id for the App on `repo_full_name` (App JWT).

    Raises `AppNotInstalledError` (with an actionable message) if the App is
    not installed -- GitHub does not permit an App to install itself; a repo
    admin must authorize it once in the GitHub UI. Only a 404 is interpreted
    this way -- any other status (e.g. a 401 from a malformed
    GITHUB_APP_PRIVATE_KEY, or a transient 5xx) raises a plain
    RuntimeError instead, so a genuine auth/API error is never misdiagnosed as
    a missing installation. `AppNotInstalledError` subclasses RuntimeError, so
    a caller that only catches RuntimeError still sees both cases, but one
    that wants to branch on "not installed" specifically (as
    check_installation_and_webhook does) can catch the narrower type first.

    Uses the raw requester (``GET /repos/{repo}/installation``) rather than a
    typed PyGithub method: the installed PyGithub version's ``Repository``
    class has no ``get_installation()`` method for this endpoint.
    """
    gh = _app_jwt_client()
    try:
        _, data = gh.requester.requestJsonAndCheck(
            "GET", f"/repos/{repo_full_name}/installation"
        )
    except GithubException as exc:
        if exc.status == 404:
            raise AppNotInstalledError(
                f"GitHub App is not installed on {repo_full_name}: install it once via the "
                f"GitHub UI (repo Settings -> GitHub Apps), then redeploy. ({exc.status})"
            ) from exc
        raise RuntimeError(
            f"GitHub App installation lookup for {repo_full_name} failed with "
            f"{exc.status} ({exc.data}) -- likely a bad GITHUB_APP_ID or "
            f"GITHUB_APP_PRIVATE_KEY, not a missing App installation."
        ) from exc
    return int(data["id"])


def discover_installation_id_for_app(client: Github | None = None) -> int:
    """Return the App's single installation id (GET /app/installations, App JWT).

    This project's scope (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md)
    is one GitHub account/org per App installation -- so, unlike
    discover_installation_id(repo), this needs no repo to seed the lookup and
    works whether or not GITHUB_TARGET_REPO is configured.

    Raises `AppNotInstalledError` if the App has no installations at all.
    Raises a plain `RuntimeError` naming every installation's account login if
    there is more than one -- that's the out-of-scope cross-org case; an
    operator must pin GITHUB_APP_INSTALLATION_ID explicitly rather than have
    one silently chosen for them. Any other API failure (e.g. a 401 from a
    malformed GITHUB_APP_PRIVATE_KEY, or a transient 5xx) also raises a plain
    RuntimeError, chained from the underlying GithubException so a caller can
    still recover the HTTP status -- mirroring discover_installation_id's own
    non-404 handling, so a genuine auth/API error is never misdiagnosed.

    `client`, when given, is used instead of _app_jwt_client() -- lets the
    dashboard's guided credential flow validate a candidate App identity that
    was never written to Settings.
    """
    gh = client if client is not None else _app_jwt_client()
    try:
        _, data = gh.requester.requestJsonAndCheck("GET", "/app/installations")
    except GithubException as exc:
        raise RuntimeError(
            f"GitHub App installations lookup failed with {exc.status} ({exc.data}) -- "
            "likely a bad GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY."
        ) from exc
    if not data:
        raise AppNotInstalledError(
            "GitHub App has no installations: install it once via the GitHub UI "
            "(repo or org Settings -> GitHub Apps), then redeploy."
        )
    if len(data) > 1:
        accounts = ", ".join(installation["account"]["login"] for installation in data)
        raise RuntimeError(
            f"GitHub App has multiple installations ({accounts}) -- set "
            "GITHUB_APP_INSTALLATION_ID explicitly to pick one."
        )
    return int(data[0]["id"])


def discover_and_verify_installation_id(expected: int) -> int:
    """Resolve the App's actual installation id and confirm it matches `expected`.

    This project requires GITHUB_APP_INSTALLATION_ID to be configured
    explicitly, never guessed on the operator's behalf -- so a pinned value
    that no longer matches the account's actual installation (most likely
    because the App was uninstalled and reinstalled, which GitHub assigns a
    new id for) is exactly as broken as no installation at all, and must
    fail the same way: loudly, not silently patched over.

    Propagates `AppNotInstalledError`/`RuntimeError` from
    discover_installation_id_for_app() unchanged (no installation, or more
    than one). Raises a distinct `RuntimeError` on a mismatch. Returns the
    freshly-discovered id (== `expected`) otherwise.
    """
    discovered = discover_installation_id_for_app()
    if discovered != expected:
        raise RuntimeError(
            f"GITHUB_APP_INSTALLATION_ID={expected} does not match the App's actual "
            f"installation id={discovered} -- the App was likely uninstalled and "
            "reinstalled. Update GITHUB_APP_INSTALLATION_ID to the new value."
        )
    return discovered


def list_installation_repos(installation_id: int) -> list[str]:
    """Full names of repos `installation_id`'s token can access (GET
    /installation/repositories, up to 100 per page -- still first page only,
    a cheap mitigation against a false "not covered" report for an
    allowlisted repo that happens to sit past the default page size, not a
    full pagination implementation).

    Takes `installation_id` explicitly rather than reading
    `settings.github_app_installation_id` -- scripts/deploy.py's
    check_installation_and_webhook calls this immediately after discovering a
    fresh id via discover_installation_id_for_app(), before that setting is
    ever assigned (only main.py's boot path assigns it). Reading the
    setting here would 404 on every unpinned first deploy.

    Used by scripts/deploy.py's github-app check for display/verification of a
    configured GITHUB_TARGET_REPO allowlist -- not a security boundary. The
    webhook's legitimacy guarantee comes from HMAC verification
    (webhook.py), not from this list.
    """
    gh = get_installation_client(installation_id)
    _, data = gh.requester.requestJsonAndCheck(
        "GET", "/installation/repositories", parameters={"per_page": 100}
    )
    return [repo["full_name"] for repo in data.get("repositories", [])]


def repos_not_covered(covered: list[str], repos: frozenset[str]) -> list[str]:
    """Entries of `repos` absent from `covered`, sorted, comparing
    case-insensitively (GitHub repo names are case-insensitive, so an
    allowlist entry need not match `covered`'s reported casing exactly).
    Empty if `repos` is empty -- nothing configured means nothing to verify.

    Shared by scripts/deploy.py's github-app check (which also fixes the
    webhook afterward) and scripts/doctor.py's read-only equivalent, so this
    comparison itself can never have two implementations to drift apart --
    doctor.py's module docstring calls that out as the thing most worth
    avoiding. Takes `covered` already-fetched rather than an installation_id,
    since both callers already have it (list_installation_repos is not
    cheap enough to call twice per check).
    """
    if not repos:
        return []
    covered_casefold = {c.casefold() for c in covered}
    return sorted(r for r in repos if r.casefold() not in covered_casefold)


_PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}


def get_app_permissions() -> tuple[dict[str, str], list[str]]:
    """(permissions, events) the App ACTUALLY has recorded on GitHub right
    now (GET /app, App JWT) -- not what create_github_app.py's manifest
    originally requested at creation time. An operator can edit an App's
    permissions or event subscriptions by hand in GitHub's UI at any point
    after creation, whether the App itself was created by the manifest flow
    or entirely by hand; this is what lets scripts/doctor.py's
    app-permissions check catch that drift either way.
    """
    gh = _app_jwt_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/app")
    return data.get("permissions", {}), data.get("events", [])


def diff_app_permissions(
    actual_permissions: dict[str, str],
    actual_events: list[str],
    wanted_permissions: dict[str, str],
    wanted_events: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """(under, over): human-readable lines describing every permission/event
    gap against `wanted_permissions`/`wanted_events`, in EITHER direction.

    under-permissioned entries are a functional risk -- the bot cannot do
    something its own code needs (e.g. it can't post a review comment
    without `issues: write`). over-permissioned entries are a
    least-privilege nit -- the App can do more than this project's code ever
    uses, which is a real (if smaller) risk of its own, but not something
    that breaks the bot. scripts/doctor.py's app-permissions check reports
    the first as FAIL and the second as WARN.
    """
    under: list[str] = []
    over: list[str] = []

    for name, wanted_level in wanted_permissions.items():
        actual_level = actual_permissions.get(name)
        actual_rank = _PERMISSION_LEVELS.get(actual_level or "", 0)
        wanted_rank = _PERMISSION_LEVELS[wanted_level]
        if actual_rank < wanted_rank:
            under.append(f"{name}: have {actual_level or '(none)'}, need {wanted_level}")
        elif actual_rank > wanted_rank:
            over.append(f"{name}: have {actual_level}, only need {wanted_level}")
    extra_permissions = sorted(set(actual_permissions) - set(wanted_permissions))
    over.extend(
        f"{name}: have {actual_permissions[name]}, not used at all" for name in extra_permissions
    )

    missing_events = sorted(set(wanted_events) - set(actual_events))
    under.extend(f"event {e!r} not subscribed" for e in missing_events)
    extra_events = sorted(set(actual_events) - set(wanted_events))
    over.extend(f"event {e!r} subscribed but not used" for e in extra_events)

    return under, over


def set_webhook_url(url: str) -> None:
    """Idempotently point the App's webhook at `url` (PATCH /app/hook/config, App JWT)."""
    gh = _app_jwt_client()
    gh.requester.requestJsonAndCheck("PATCH", "/app/hook/config", input={"url": url})


def get_webhook_url() -> str:
    """Return the App's currently configured webhook URL (App JWT).

    Returns "" when the App has no webhook URL set, which is the genuine
    first-deploy state rather than an error. Any API failure propagates as
    GithubException so the caller can decline to write after a failed read.

    Only an absolute http(s) URL counts as configured. PyGithub's
    Requester.__postProcess injects a synthetic ``url`` key -- the literal
    request path -- into any GET response dict that lacks one, so an
    unconfigured webhook arrives here as {"url": "/app/hook/config"} rather
    than {}. A webhook URL is by definition absolute, so requiring that scheme
    rejects the synthetic value without depending on PyGithub's internals or
    hard-coding the path it happens to inject.
    """
    gh = _app_jwt_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/hook/config")
    url = (data or {}).get("url") or ""
    return url if url.startswith(("http://", "https://")) else ""


@dataclass
class PrDiff:
    text: str
    # GitHub's canonical name for the repo, which may differ from the
    # `repo_full_name` requested if the repo was renamed since that value
    # was stored -- GitHub transparently redirects old-name requests rather
    # than erroring, so this is the only way a caller can notice.
    repo_full_name: str
    # The PR's CURRENT draft status (not a snapshot from whenever a webhook
    # last fired) -- fetched for free off the same PullRequest object
    # already needed for the diff, so a live draft check costs no extra call.
    draft: bool


def fetch_pr_diff(repo_full_name: str, pr_number: int) -> PrDiff:
    """Fetch a PR's raw unified diff text.

    Built by concatenating each changed file's patch (as returned by the
    GitHub API), rather than requesting the `.diff` media type directly —
    PyGithub's typed `PullRequest.get_files()` keeps this dependency-free.
    Annotation (file:line) and token-capping happen later, in diff_utils.py.
    """
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    chunks: list[str] = []
    for f in pr.get_files():
        header = f"diff --git a/{f.filename} b/{f.filename}"
        patch = f.patch if f.patch else "(binary file or no textual diff available)"
        chunks.append(f"{header}\n{patch}")
    return PrDiff(text="\n".join(chunks), repo_full_name=repo.full_name, draft=pr.draft)


def react_eyes_to_pr(repo_full_name: str, pr_number: int) -> None:
    """React to the PR itself with 👀, mirroring CodeRabbit's "I've seen your
    push" ack -- immediate feedback that the webhook registered, well before
    the actual review (dispatched separately, possibly much later) posts.

    A PR is exposed via the Issues API for reactions (there is no PR-level
    reactions endpoint), hence ``as_issue()``. GitHub's reactions API is
    idempotent per (actor, content) pair, so a repeat call for the same PR
    (e.g. a second ``synchronize`` before the first review lands) just
    no-ops rather than stacking duplicate reactions.
    """
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    pr.as_issue().create_reaction("eyes")


def upsert_comment(
    repo_full_name: str, pr_number: int, body: str, comment_id: int | None = None
) -> IssueComment:
    """Find the bot's own comment (by id, else author-filtered marker scan) and edit
    it in place; else create one. Returns the resulting IssueComment."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    marked_body = body if COMMENT_MARKER in body else f"{COMMENT_MARKER}\n{body}"
    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        existing.edit(marked_body)
        return existing
    return pr.create_issue_comment(marked_body)


def append_review_footnote(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append a failure footnote below the bot's own comment, preserving the review.
    Finds the comment by id then author-filtered marker scan; creates a
    marker-carrying comment if none exists."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        base = _strip_existing_footnote(existing.body)
        existing.edit(f"{base}\n\n{footnote}")
        return existing
    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")


def append_schedule_notice(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append/refresh the schedule-wait footnote below the bot's own comment,
    preserving the review. Finds the comment by id then author-filtered marker
    scan; creates a marker-carrying comment if none exists."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        base = _strip_existing_footnote(existing.body)
        existing.edit(f"{base}\n\n{footnote}")
        return existing
    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")


def clear_schedule_notice(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> IssueComment | None:
    """Strip any existing footnote (schedule note or, defensively, failure
    note) from the bot's comment -- called once a deferred ticket is claimed
    and its wait is over. No-op (no edit call) if the comment has no footnote
    to strip, or if no bot comment exists yet."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is None:
        return None
    stripped = _strip_existing_footnote(existing.body)
    if stripped != existing.body.rstrip():
        existing.edit(stripped)
    return existing
