"""Deterministic, CI-safe tests for github_app.py.

PyGithub makes its HTTP calls via the `requests` library (not `httpx`), so
`respx` — which only intercepts `httpx` traffic — cannot see them. To still
get respx-style deterministic mocking with zero real network calls and zero
new dependencies, these tests patch `requests.adapters.HTTPAdapter.send`
(the actual transport boundary PyGithub's Requester sends through) with a
tiny in-memory router keyed on method + URL substring.
"""

import base64
import json
import time

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_app
from config import settings

REPO_FULL_NAME = "test-owner/pr-review-bot-testbed"
PR_NUMBER = 1
REPO_API_URL = f"https://api.github.com/repos/{REPO_FULL_NAME}"
PR_API_URL = f"{REPO_API_URL}/pulls/{PR_NUMBER}"
ISSUE_API_URL = f"{REPO_API_URL}/issues/{PR_NUMBER}"

_seen_key_material: list[str] = []


def _repo_json():
    return {
        "id": 1,
        "name": "pr-review-bot-testbed",
        "full_name": REPO_FULL_NAME,
        "url": REPO_API_URL,
    }


def _pull_json():
    # PyGithub's PullRequest.get_issue_comments()/create_issue_comment() need
    # `issue_url`, and get_files()/get_pull() completion needs `url` — both
    # are lazily fetched from this response, so both must be present.
    return {
        "number": PR_NUMBER,
        "id": 1,
        "title": "test PR",
        "state": "open",
        "draft": False,
        "url": PR_API_URL,
        "issue_url": ISSUE_API_URL,
    }


@pytest.fixture(scope="module")
def _app_credentials_key_material() -> str:
    """Generates the throwaway RSA key once per test file, not once per
    test. No test depends on the key's value differing from another
    test's -- see _throwaway_app_credentials below."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


@pytest.fixture(autouse=True)
def _throwaway_app_credentials(_app_credentials_key_material, monkeypatch):
    """Point settings at the module's shared throwaway RSA key.

    Keeps these tests independent of the real (gitignored) App credentials —
    only JWT *signing* happens locally with this key; every HTTP call is
    mocked below, so nothing is ever sent anywhere with it. The key itself
    can't be generated at module scope directly: `monkeypatch` is only
    available at function scope, and a module-scoped fixture depending on it
    raises ScopeMismatch at collection. So the expensive part (keygen) is
    module-scoped via _app_credentials_key_material above, and this fixture
    just does the (cheap) monkeypatch.setattr calls every test.
    """
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)
    monkeypatch.setattr(settings, "github_app_private_key", _app_credentials_key_material)


@pytest.fixture(autouse=True)
def _no_pygithub_rate_limit_sleep(monkeypatch):
    """PyGithub's Requester.__deferRequest() calls a real time.sleep() to
    keep requests at least Consts.DEFAULT_SECONDS_BETWEEN_REQUESTS (0.25s)
    apart, and writes at least ...WRITES (1.0s) apart -- a real safety
    throttle against GitHub's secondary rate limits in production. Every
    call in this file goes through the fully-mocked fake_transport, so the
    throttle protects nothing here and only wastes wall-clock: profiled at
    46 real time.sleep calls totaling 11.5s across this file's 33 tests.
    Patching stdlib time.sleep (not github_app.py, which this leaves
    untouched) is the narrowest fix -- PyGithub's own pacing math still
    runs, it just no longer blocks.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.mark.xdist_group(name="github_app_key_material")
def test_key_material_fixture_produces_a_value(_app_credentials_key_material):
    _seen_key_material.append(_app_credentials_key_material)
    assert _app_credentials_key_material  # non-empty base64 string


@pytest.mark.xdist_group(name="github_app_key_material")
def test_key_material_fixture_is_shared_not_regenerated(_app_credentials_key_material):
    assert _seen_key_material, "test_key_material_fixture_produces_a_value must run first"
    assert _app_credentials_key_material == _seen_key_material[0]


class FakeGithubTransport:
    """Routes requests by (method, url-substring) to canned JSON responses."""

    def __init__(self):
        self.routes: list[tuple[str, str, dict, int]] = []
        self.requests: list[requests.PreparedRequest] = []

    def route(self, method: str, url_substring: str, json_body, status_code: int = 200):
        self.routes.append((method.upper(), url_substring, json_body, status_code))

    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        self.requests.append(request)
        # Longest url_substring first, so e.g. ".../pulls/1/files" doesn't
        # get shadowed by an earlier, shorter ".../pulls/1" registration.
        for method, url_substring, json_body, status_code in sorted(
            self.routes, key=lambda r: -len(r[1])
        ):
            if request.method == method and url_substring in request.url:
                return self._build_response(request, json_body, status_code)
        raise AssertionError(f"Unmocked request: {request.method} {request.url}")

    @staticmethod
    def _build_response(request, json_body, status_code) -> requests.Response:
        resp = requests.Response()
        resp.status_code = status_code
        resp.headers["Content-Type"] = "application/json"
        resp._content = json.dumps(json_body).encode("utf-8")
        resp.encoding = "utf-8"
        resp.url = request.url
        resp.reason = "OK"
        resp.request = request
        return resp


@pytest.fixture
def fake_transport(monkeypatch):
    transport = FakeGithubTransport()
    transport.route(
        "POST",
        f"/app/installations/{123456}/access_tokens",
        {"token": "fake-installation-token", "expires_at": "2099-01-01T00:00:00Z"},
        201,
    )
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport.send)
    return transport


def test_upsert_comment_does_not_pay_pygithubs_real_rate_limit_sleep(fake_transport):
    """PyGithub's Requester enforces real time.sleep() calls between requests
    (Consts.DEFAULT_SECONDS_BETWEEN_REQUESTS=0.25s) and before writes
    (...WRITES=1.0s) to avoid tripping GitHub's real secondary rate limits.
    That pacing is pointless against this file's fully-mocked transport --
    profiled via cProfile at 46 real time.sleep calls totaling 11.5s across
    this file's 33 tests. upsert_comment makes 4 calls on one client
    (token exchange, GET repo, GET pull, GET comments, POST comment) --
    enough to trigger multiple real waits if left unpatched."""
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments", [])
    fake_transport.route(
        "POST",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        {"id": 1, "body": "x", "user": {"login": "bot"}},
        201,
    )

    start = time.monotonic()
    github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "hello")
    elapsed = time.monotonic() - start

    assert elapsed < 0.3, (
        f"took {elapsed:.2f}s -- PyGithub's real inter-request rate-limit sleep "
        "is not being neutralized in this test file"
    )


def test_react_eyes_to_pr_posts_an_eyes_reaction_to_the_issues_endpoint(fake_transport):
    """A PR has no PR-level reactions endpoint -- reactions go through the
    Issues API (as_issue()), which is why the POST target is /issues/{n}/
    reactions, not /pulls/{n}/reactions.

    as_issue() constructs a non-lazy Issue, whose __init__ immediately fires
    a GET to complete its attributes -- unmocked, that GET falls through to
    the (substring-matching) repo route below and comes back with the
    repo's own "url" field, silently overwriting the Issue's url and
    misdirecting the reaction POST at the repo instead of the issue. This
    route is what that completing GET actually needs.
    """
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}",
        {"id": 1, "number": PR_NUMBER, "url": ISSUE_API_URL},
    )
    fake_transport.route(
        "POST",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/reactions",
        {"id": 1, "content": "eyes"},
        201,
    )

    github_app.react_eyes_to_pr(REPO_FULL_NAME, PR_NUMBER)

    posted = [
        r for r in fake_transport.requests
        if r.method == "POST" and r.url.endswith(f"/issues/{PR_NUMBER}/reactions")
    ]
    assert len(posted) == 1
    assert json.loads(posted[0].body) == {"content": "eyes"}


def test_fetch_pr_diff_concatenates_file_patches(fake_transport):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}/files",
        [
            {
                "sha": "abc",
                "filename": "app.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ -1 +1 @@\n-old\n+new",
            }
        ],
    )

    diff = github_app.fetch_pr_diff(REPO_FULL_NAME, PR_NUMBER)

    assert "diff --git a/app.py b/app.py" in diff.text
    assert "-old" in diff.text
    assert "+new" in diff.text
    assert diff.repo_full_name == REPO_FULL_NAME
    assert diff.draft is False


def test_fetch_pr_diff_reports_a_draft_pr(fake_transport):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route(
        "GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", {**_pull_json(), "draft": True}
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}/files", [])

    diff = github_app.fetch_pr_diff(REPO_FULL_NAME, PR_NUMBER)

    assert diff.draft is True


def test_fetch_pr_diff_returns_the_canonical_repo_name_after_a_rename(fake_transport):
    """GitHub transparently redirects a renamed repo's old-name requests
    (no error) -- the only way to notice is that the repo object it returns
    carries the CURRENT full_name, not the one we requested."""
    renamed = {**_repo_json(), "full_name": "test-owner/renamed-repo"}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", renamed)
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}/files", [])

    diff = github_app.fetch_pr_diff(REPO_FULL_NAME, PR_NUMBER)

    assert diff.repo_full_name == "test-owner/renamed-repo"


def test_upsert_comment_creates_when_no_marker_comment_exists(fake_transport, monkeypatch):
    created = {}

    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 111,
                "body": "an unrelated human comment, no marker here",
                "user": {"login": "someone", "type": "User"},
            }
        ],
    )

    def send_with_create_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 222, "body": body["body"], "user": {"login": "bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_create_capture)
    )

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nfindings here")

    assert github_app.COMMENT_MARKER in created["body"]
    assert "findings here" in created["body"]
    assert result.id == 222


def test_upsert_comment_edits_existing_marker_comment_in_place(fake_transport, monkeypatch):
    edited = {}
    existing_body = f"{github_app.COMMENT_MARKER}\n## Review\nold findings"

    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 333,
                "body": existing_body,
                "user": {"login": "bot", "type": "Bot"},
                "url": f"{REPO_API_URL}/issues/comments/333",
            }
        ],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            raise AssertionError("should not create a new comment when marker already exists")
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew findings")

    assert github_app.COMMENT_MARKER in edited["body"]
    assert "new findings" in edited["body"]
    assert "old findings" not in edited["body"]
    assert result.id == 333


def test_append_review_footnote_edits_marker_and_replaces_prior_footnote(
    fake_transport, monkeypatch
):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.FAIL_NOTE_START}\n> old failure note\n{github_app.FAIL_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    footnote = f"{github_app.FAIL_NOTE_START}\n> new failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert "good findings" in edited["body"]                 # review preserved
    assert "new failure note" in edited["body"]
    assert "old failure note" not in edited["body"]          # prior footnote replaced
    assert edited["body"].count(github_app.FAIL_NOTE_START) == 1  # no stacking


def test_append_review_footnote_preserves_stray_marker_in_real_content(fake_transport, monkeypatch):
    """A stray, unmatched FAIL_NOTE_START inside real finding text (e.g. a
    specialist quoting this very file's source) must NOT be treated as the
    start of a footnote block to strip -- only a WELL-FORMED TRAILING block
    (one that actually ends the body) should be replaced. Regression test for
    the content-loss bug where a first-START-to-next-END regex spanned from
    the stray marker all the way to the real trailing footnote's END,
    deleting genuine review content in between.
    """
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\n"
        f"Finding: the string `{github_app.FAIL_NOTE_START}` appears in github_app.py "
        f"and should be reviewed for clarity.\n\n"
        f"Other real finding: consider renaming this variable.\n\n"
        f"{github_app.FAIL_NOTE_START}\n> old failure note\n{github_app.FAIL_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    footnote = f"{github_app.FAIL_NOTE_START}\n> new failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    # The stray marker's surrounding real content must survive.
    assert "appears in github_app.py" in edited["body"]
    assert "consider renaming this variable" in edited["body"]
    # Only the real trailing footnote was replaced.
    assert "new failure note" in edited["body"]
    assert "old failure note" not in edited["body"]
    assert edited["body"].count(github_app.FAIL_NOTE_START) == 2  # stray + new trailing one


def test_append_review_footnote_creates_marker_comment_when_none_exists(
    fake_transport, monkeypatch
):
    created = {}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 111,
                "body": "human comment, no marker",
                "user": {"login": "someone", "type": "User"},
            }
        ],
    )

    def send_with_create_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 222, "body": body["body"], "user": {"login": "bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_create_capture)
    )

    footnote = f"{github_app.FAIL_NOTE_START}\n> failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert github_app.COMMENT_MARKER in created["body"]
    assert "failure note" in created["body"]


def test_upsert_comment_skips_human_comment_containing_the_marker(fake_transport, monkeypatch):
    created = {}
    # A human comment that quotes the marker must NOT be edited.
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 501, "body": f"quoting the bot: {github_app.COMMENT_MARKER}",
          "user": {"login": "a-human", "type": "User"},
          "url": f"{REPO_API_URL}/issues/comments/501"}],
    )

    def send(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/501" in request.url:
            raise AssertionError("must not edit a human comment that merely quotes the marker")
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 777, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                201,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nfresh")
    assert result.id == 777                       # created a new bot comment
    assert github_app.COMMENT_MARKER in created["body"]


def test_upsert_comment_edits_by_id_when_comment_id_given(fake_transport, monkeypatch):
    edited = {}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/comments/333",
        {
            "id": 333,
            "body": f"{github_app.COMMENT_MARKER}\nold",
            "user": {"login": "bot", "type": "Bot"},
            "url": f"{REPO_API_URL}/issues/comments/333",
        },
    )

    def send(request, **kwargs):
        if request.method == "GET" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            raise AssertionError("must not scan the thread when a comment_id is known")
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew", comment_id=333)
    assert result.id == 333
    assert "new" in edited["body"]


def test_upsert_comment_falls_back_to_scan_when_comment_id_deleted(fake_transport, monkeypatch):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/issues/comments/999",
                         {"message": "Not Found"}, 404)
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 333,
                "body": f"{github_app.COMMENT_MARKER}\nold",
                "user": {"login": "bot", "type": "Bot"},
                "url": f"{REPO_API_URL}/issues/comments/333",
            }
        ],
    )

    def send(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            return fake_transport._build_response(
                request, {"id": 333, "body": json.loads(request.body)["body"],
                          "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew", comment_id=999)
    assert result.id == 333   # deleted id -> fell back to the author-filtered scan


def test_append_schedule_notice_edits_marker_and_adds_note(fake_transport, monkeypatch):
    edited = {}
    existing_body = f"{github_app.COMMENT_MARKER}\n## Review\ngood findings"
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    note = (
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~14:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    github_app.append_schedule_notice(REPO_FULL_NAME, PR_NUMBER, note)

    assert "good findings" in edited["body"]
    assert "Re-review scheduled" in edited["body"]


def test_append_schedule_notice_replaces_prior_schedule_note(fake_transport, monkeypatch):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    note = (
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~14:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    github_app.append_schedule_notice(REPO_FULL_NAME, PR_NUMBER, note)

    assert "good findings" in edited["body"]
    assert "~14:00 UTC" in edited["body"]
    assert "~10:00 UTC" not in edited["body"]
    assert edited["body"].count(github_app.SCHEDULE_NOTE_START) == 1


def test_upsert_comment_full_overwrite_removes_stale_schedule_note(fake_transport, monkeypatch):
    """A real review completion (upsert_comment's full-body overwrite) must
    wipe a previously-posted schedule note -- self-cleaning, no separate
    cleanup code needed."""
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\nold findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew findings")

    assert "new findings" in edited["body"]
    assert "old findings" not in edited["body"]
    assert "Re-review scheduled" not in edited["body"]   # schedule note wiped by full overwrite


def test_strip_existing_footnote_removes_schedule_note_when_writing_fail_note(
    fake_transport, monkeypatch
):
    """Cross-footnote robustness: append_review_footnote (fail note) must clean
    up a stale leftover schedule note, since _strip_existing_footnote now
    recognizes either marker pair."""
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    footnote = f"{github_app.FAIL_NOTE_START}\n> failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert "good findings" in edited["body"]
    assert "failure note" in edited["body"]
    assert "Re-review scheduled" not in edited["body"]   # stale schedule note stripped


def test_clear_schedule_notice_strips_note_and_edits(fake_transport, monkeypatch):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request,
                {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}},
                200,
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture)
    )

    result = github_app.clear_schedule_notice(REPO_FULL_NAME, PR_NUMBER)

    assert result.id == 333
    assert "good findings" in edited["body"]
    assert "Re-review scheduled" not in edited["body"]


def test_clear_schedule_notice_is_noop_when_no_footnote_present(fake_transport, monkeypatch):
    existing_body = f"{github_app.COMMENT_MARKER}\n## Review\ngood findings"
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_that_forbids_patch(request, **kwargs):
        if request.method == "PATCH":
            raise AssertionError("must not edit when there is no footnote to strip")
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", staticmethod(send_that_forbids_patch)
    )

    result = github_app.clear_schedule_notice(REPO_FULL_NAME, PR_NUMBER)
    assert result.id == 333


def test_clear_schedule_notice_returns_none_when_no_bot_comment_exists(fake_transport, monkeypatch):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 111,
                "body": "human comment, no marker",
                "user": {"login": "someone", "type": "User"},
            }
        ],
    )

    result = github_app.clear_schedule_notice(REPO_FULL_NAME, PR_NUMBER)
    assert result is None


def test_read_private_key_decodes_the_base64_env(monkeypatch):
    import base64

    pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
    monkeypatch.setattr(
        settings,
        "github_app_private_key",
        base64.b64encode(pem.encode()).decode(),
    )
    assert github_app._read_private_key() == pem


def test_read_private_key_rejects_malformed_base64(monkeypatch):
    """A mis-pasted raw PEM (plausible now that there's no more `_PATH`
    fallback signaling "this should be a path") must fail clearly instead of
    silently decoding to garbage. The error must name the var but never echo
    the malformed input itself (CLAUDE.md's rule on secret-bearing validation
    errors)."""
    not_base64 = "-----BEGIN KEY-----\nnot valid base64!!!\n-----END KEY-----\n"
    monkeypatch.setattr(settings, "github_app_private_key", not_base64)
    with pytest.raises(ValueError) as exc_info:
        github_app._read_private_key()
    message = str(exc_info.value)
    assert "GITHUB_APP_PRIVATE_KEY" in message
    assert not_base64 not in message


def test_discover_installation_id_returns_id(fake_transport):
    import github_app

    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/installation", {"id": 424242})
    assert github_app.discover_installation_id(REPO_FULL_NAME) == 424242


def test_discover_installation_id_non_404_is_not_misdiagnosed_as_not_installed(fake_transport):
    """A 401 (e.g. a malformed GITHUB_APP_PRIVATE_KEY) or other non-404
    status must not be reported as "not installed" -- that's a misdiagnosis
    that would send an operator chasing the wrong fix."""
    import github_app

    fake_transport.route(
        "GET", f"/repos/{REPO_FULL_NAME}/installation", {"message": "Bad credentials"}, 401
    )
    with pytest.raises(RuntimeError) as exc_info:
        github_app.discover_installation_id(REPO_FULL_NAME)
    assert "not installed" not in str(exc_info.value)
    assert "401" in str(exc_info.value)


def test_set_webhook_url_patches_hook_config(fake_transport):
    import github_app

    fake_transport.route("PATCH", "/app/hook/config", {"url": "https://x/webhook"})
    github_app.set_webhook_url("https://x/webhook")  # must not raise


def test_discover_installation_id_raises_app_not_installed_on_404(fake_transport):
    """A distinct type lets callers branch without matching on message text."""
    fake_transport.route(
        "GET", f"/repos/{REPO_FULL_NAME}/installation", {"message": "Not Found"}, 404
    )
    with pytest.raises(github_app.AppNotInstalledError):
        github_app.discover_installation_id(REPO_FULL_NAME)


def test_discover_installation_id_non_404_is_not_app_not_installed(fake_transport):
    """A 401 from a bad key must not be reported as a missing installation."""
    fake_transport.route(
        "GET", f"/repos/{REPO_FULL_NAME}/installation", {"message": "Bad credentials"}, 401
    )
    with pytest.raises(RuntimeError) as excinfo:
        github_app.discover_installation_id(REPO_FULL_NAME)
    assert not isinstance(excinfo.value, github_app.AppNotInstalledError)


def test_discover_installation_id_for_app_returns_id_with_one_installation(fake_transport):
    fake_transport.route(
        "GET", "/app/installations", [{"id": 555, "account": {"login": "someone"}}]
    )
    assert github_app.discover_installation_id_for_app() == 555


def test_discover_installation_id_for_app_raises_app_not_installed_when_empty(fake_transport):
    fake_transport.route("GET", "/app/installations", [])
    with pytest.raises(github_app.AppNotInstalledError):
        github_app.discover_installation_id_for_app()


def test_discover_installation_id_for_app_raises_when_ambiguous(fake_transport):
    fake_transport.route(
        "GET",
        "/app/installations",
        [
            {"id": 1, "account": {"login": "org-a"}},
            {"id": 2, "account": {"login": "org-b"}},
        ],
    )
    with pytest.raises(RuntimeError) as exc_info:
        github_app.discover_installation_id_for_app()
    message = str(exc_info.value)
    assert "org-a" in message
    assert "org-b" in message
    assert "GITHUB_APP_INSTALLATION_ID" in message


def test_discover_and_verify_installation_id_returns_when_matching(fake_transport):
    fake_transport.route(
        "GET", "/app/installations", [{"id": 555, "account": {"login": "someone"}}]
    )
    assert github_app.discover_and_verify_installation_id(555) == 555


def test_discover_and_verify_installation_id_raises_on_mismatch(fake_transport):
    fake_transport.route(
        "GET", "/app/installations", [{"id": 555, "account": {"login": "someone"}}]
    )
    with pytest.raises(RuntimeError) as exc_info:
        github_app.discover_and_verify_installation_id(111)
    message = str(exc_info.value)
    assert "111" in message
    assert "555" in message
    assert "GITHUB_APP_INSTALLATION_ID" in message


def test_discover_and_verify_installation_id_propagates_app_not_installed(fake_transport):
    fake_transport.route("GET", "/app/installations", [])
    with pytest.raises(github_app.AppNotInstalledError):
        github_app.discover_and_verify_installation_id(555)


def test_discover_installation_id_for_app_wraps_a_non_404_github_error(fake_transport):
    """Mirrors discover_installation_id's own non-404 handling: a 401/5xx must
    become an actionable RuntimeError, not propagate as a raw PyGithub
    exception (which scripts/deploy.py's github-app check would otherwise
    only be able to report as an opaque 'unexpected <ExceptionType>')."""
    fake_transport.route("GET", "/app/installations", {"message": "Bad credentials"}, 401)
    with pytest.raises(RuntimeError) as exc_info:
        github_app.discover_installation_id_for_app()
    assert not isinstance(exc_info.value, github_app.AppNotInstalledError)
    assert "401" in str(exc_info.value)


def test_app_jwt_client_for_builds_a_working_client(fake_transport, _app_credentials_key_material):
    """A client built for an ARBITRARY app id + key (never touching Settings)
    must still work against the same mocked transport -- proves the
    dashboard's guided credential flow can validate a candidate identity
    that was never written to Settings/Render."""
    fake_transport.route(
        "GET", "/app/installations", [{"id": 777, "account": {"login": "candidate-org"}}]
    )
    client = github_app._app_jwt_client_for(123456, _app_credentials_key_material)
    assert github_app.discover_installation_id_for_app(client=client) == 777


def test_app_jwt_client_for_rejects_bad_base64():
    with pytest.raises(ValueError, match="not valid base64"):
        github_app._app_jwt_client_for(123456, "not-valid-base64!!!")


def test_discover_installation_id_for_app_uses_default_client_when_none_given(fake_transport):
    """The existing no-arg call sites (deploy.py, set_override.py, etc.) must
    keep working unchanged after adding the optional `client` param."""
    fake_transport.route(
        "GET", "/app/installations", [{"id": 555, "account": {"login": "someone"}}]
    )
    assert github_app.discover_installation_id_for_app(client=None) == 555


def test_list_installation_repos_returns_full_names(fake_transport):
    fake_transport.route(
        "GET",
        "/installation/repositories",
        {
            "total_count": 2,
            "repositories": [
                {"full_name": "someone/repo-a"},
                {"full_name": "someone/repo-b"},
            ],
        },
    )
    assert github_app.list_installation_repos(123456) == ["someone/repo-a", "someone/repo-b"]


def test_repos_not_covered_is_empty_when_nothing_configured():
    assert github_app.repos_not_covered(["owner/a"], frozenset()) == []


def test_repos_not_covered_names_only_the_missing_entries():
    missing = github_app.repos_not_covered(
        ["owner/a"], frozenset({"owner/a", "owner/missing"})
    )
    assert missing == ["owner/missing"]


def test_repos_not_covered_matches_case_insensitively():
    assert github_app.repos_not_covered(["owner/repo"], frozenset({"Owner/Repo"})) == []


def test_list_installation_repos_uses_the_given_installation_id_not_settings(
    fake_transport, monkeypatch
):
    """Regression test: list_installation_repos() must use the installation_id
    it was given, not settings.github_app_installation_id -- scripts/deploy.py
    calls this immediately after discovering a fresh id via
    discover_installation_id_for_app(), before that setting is ever assigned.
    Reading the setting instead 404s on every unpinned first deploy."""
    monkeypatch.setattr(settings, "github_app_installation_id", 999999)  # deliberately wrong
    fake_transport.route(
        "POST",
        "/app/installations/555/access_tokens",
        {"token": "fake-installation-token-for-555", "expires_at": "2099-01-01T00:00:00Z"},
        201,
    )
    fake_transport.route(
        "GET",
        "/installation/repositories",
        {"total_count": 1, "repositories": [{"full_name": "someone/repo-a"}]},
    )

    result = github_app.list_installation_repos(555)

    assert result == ["someone/repo-a"]
    token_requests = [
        r for r in fake_transport.requests
        if r.method == "POST" and "access_tokens" in r.url
    ]
    assert any("/app/installations/555/access_tokens" in r.url for r in token_requests)
    assert not any("/app/installations/999999/access_tokens" in r.url for r in token_requests)


def test_get_app_permissions_returns_permissions_and_events(fake_transport):
    fake_transport.route(
        "GET", "/app",
        {"permissions": {"issues": "write", "contents": "read"}, "events": ["pull_request"]},
    )
    permissions, events = github_app.get_app_permissions()
    assert permissions == {"issues": "write", "contents": "read"}
    assert events == ["pull_request"]


def test_get_app_permissions_defaults_missing_fields_to_empty(fake_transport):
    fake_transport.route("GET", "/app", {})
    assert github_app.get_app_permissions() == ({}, [])


def test_diff_app_permissions_reports_nothing_when_exactly_matching():
    under, over = github_app.diff_app_permissions(
        {"issues": "write"}, ["pull_request"], {"issues": "write"}, ("pull_request",)
    )
    assert under == []
    assert over == []


def test_diff_app_permissions_flags_a_missing_permission_as_under():
    under, over = github_app.diff_app_permissions({}, [], {"issues": "write"}, ())
    assert any("issues" in line and "need write" in line for line in under)
    assert over == []


def test_diff_app_permissions_flags_a_weaker_permission_as_under():
    under, _over = github_app.diff_app_permissions(
        {"issues": "read"}, [], {"issues": "write"}, ()
    )
    assert any("have read" in line and "need write" in line for line in under)


def test_diff_app_permissions_flags_a_broader_permission_as_over_not_under():
    _under, over = github_app.diff_app_permissions(
        {"issues": "admin"}, [], {"issues": "write"}, ()
    )
    assert any("have admin" in line for line in over)
    assert _under == []


def test_diff_app_permissions_flags_an_unrequested_extra_permission_as_over():
    _under, over = github_app.diff_app_permissions(
        {"issues": "write", "administration": "write"}, [], {"issues": "write"}, ()
    )
    assert any("administration" in line for line in over)


def test_diff_app_permissions_flags_a_missing_event_as_under():
    under, _over = github_app.diff_app_permissions({}, [], {}, ("pull_request",))
    assert any("pull_request" in line for line in under)


def test_diff_app_permissions_flags_an_extra_event_as_over():
    _under, over = github_app.diff_app_permissions(
        {}, ["pull_request", "issues"], {}, ("pull_request",)
    )
    assert any("issues" in line for line in over)


def test_get_webhook_url_returns_the_configured_url(fake_transport):
    fake_transport.route("GET", "/app/hook/config", {"url": "https://x.test/webhook"})
    assert github_app.get_webhook_url() == "https://x.test/webhook"


def test_get_webhook_url_returns_empty_when_never_configured(fake_transport):
    """An App whose webhook was never set is the genuine first-deploy state,
    not an error."""
    fake_transport.route("GET", "/app/hook/config", {})
    assert github_app.get_webhook_url() == ""


def test_get_webhook_url_ignores_a_non_absolute_url(fake_transport):
    """PyGithub injects the request path as a synthetic `url` when the response
    has none, so only an absolute http(s) URL counts as configured."""
    fake_transport.route("GET", "/app/hook/config", {"url": "/app/hook/config"})
    assert github_app.get_webhook_url() == ""
