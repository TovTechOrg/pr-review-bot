import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from config import settings
import github_app
from main import app
from providers import active
import webhook
from review_queue import store

TEST_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
def _isolate(db, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(active, "_override", "groq")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    # Default no-op: every enqueue-path test below goes through this call
    # now, and without a mock it would attempt a real GitHub API call (no
    # real App credentials in this test environment) instead of just being
    # irrelevant to whatever that test is actually checking. Tests that
    # care about this call specifically override it themselves.
    monkeypatch.setattr(github_app, "react_eyes_to_pr", lambda repo_full_name, pr_number: None)
    webhook.reset_dedup_cache()
    yield
    webhook.reset_dedup_cache()


async def _client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_valid_signature_returns_202():
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "11111111-1111-1111-1111-111111111111",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 202


async def test_invalid_signature_returns_401():
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
        "X-GitHub-Delivery": "22222222-2222-2222-2222-222222222222",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401


async def test_missing_signature_header_returns_401():
    body = b'{"action": "opened"}'
    headers = {"X-GitHub-Delivery": "33333333-3333-3333-3333-333333333333"}
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401


async def test_replayed_delivery_id_is_noop(monkeypatch):
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "44444444-4444-4444-4444-444444444444",
    }
    async with await _client() as c:
        first = await c.post("/webhook", content=body, headers=headers)
        second = await c.post("/webhook", content=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200


async def test_a_delivery_that_fails_to_enqueue_can_be_redelivered(monkeypatch):
    """A delivery id must only be marked seen once it's fully processed --
    if store.enqueue_or_update raises (e.g. Postgres briefly unreachable),
    GitHub's own Redeliver (same X-GitHub-Delivery id) must reach the
    handler again, not get a false 200 'already processed' from the dedup
    cache with the PR never actually enqueued."""
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 42, "head": {"sha": "def456"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    }

    original_enqueue = store.enqueue_or_update

    def _boom(**kwargs):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "enqueue_or_update", _boom)
    async with await _client() as c:
        with pytest.raises(RuntimeError):
            await c.post("/webhook", content=body, headers=headers)

    monkeypatch.setattr(store, "enqueue_or_update", original_enqueue)
    async with await _client() as c:
        retry = await c.post("/webhook", content=body, headers=headers)

    assert retry.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.pr_number == 42


async def test_opened_action_enqueues_ticket():
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "55555555-5555-5555-5555-555555555555",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 7
    assert ticket.head_sha == "abc123"


async def test_opened_action_reacts_with_eyes(monkeypatch):
    """Mirrors CodeRabbit's own immediate-ack reaction -- fires for the same
    actions that trigger a review, before the (possibly much later) actual
    review is dispatched."""
    reacted = []
    monkeypatch.setattr(
        github_app, "react_eyes_to_pr",
        lambda repo_full_name, pr_number: reacted.append((repo_full_name, pr_number)),
    )
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert reacted == [("owner/repo", 7)]


async def test_react_eyes_failure_does_not_block_enqueue(monkeypatch):
    """Best-effort: a broken/rate-limited reaction call must never prevent
    the durable enqueue, which is the part that actually matters."""

    def _boom(repo_full_name, pr_number):
        raise RuntimeError("reactions API unavailable")

    monkeypatch.setattr(github_app, "react_eyes_to_pr", _boom)
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 8, "head": {"sha": "def456"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.pr_number == 8


async def test_closed_action_does_not_react_with_eyes(monkeypatch, db_query):
    """The reaction is only for review-triggering actions -- a cancel must
    not react, since nothing new is actually being reviewed."""
    reacted = []
    monkeypatch.setattr(
        github_app, "react_eyes_to_pr",
        lambda repo_full_name, pr_number: reacted.append((repo_full_name, pr_number)),
    )
    opened_payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
    }
    closed_payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
    }
    async with await _client() as c:
        await c.post(
            "/webhook",
            content=json.dumps(opened_payload).encode(),
            headers={
                "X-Hub-Signature-256": _sign(json.dumps(opened_payload).encode()),
                "X-GitHub-Delivery": "88888888-8888-8888-8888-888888888888",
            },
        )
        reacted.clear()
        await c.post(
            "/webhook",
            content=json.dumps(closed_payload).encode(),
            headers={
                "X-Hub-Signature-256": _sign(json.dumps(closed_payload).encode()),
                "X-GitHub-Delivery": "99999999-9999-9999-9999-999999999999",
            },
        )

    assert reacted == []


async def test_ready_for_review_action_enqueues_ticket():
    """GitHub fires this independent of any push -- the only way a draft PR
    marked ready with zero new commits still gets reviewed (ISSUES.md's
    'Draft PRs are reviewed identically to ready-for-review PRs' gap)."""
    payload = {
        "action": "ready_for_review",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 8, "head": {"sha": "abc123"}, "draft": False},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 8


async def test_edited_action_with_base_change_enqueues_ticket():
    """A base-branch retarget can change the effective diff entirely --
    ISSUES.md's 'pull_request.edited never triggers a re-review' gap."""
    payload = {
        "action": "edited",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
        "changes": {"base": {"ref": {"from": "old-branch"}, "sha": {"from": "oldsha"}}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "88888888-8888-8888-8888-888888888888",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 9


async def test_edited_action_without_base_change_does_not_enqueue():
    """A title/body-only edit must stay a no-op -- only a base retarget
    changes the effective diff."""
    payload = {
        "action": "edited",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
        "changes": {"title": {"from": "old title"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "99999999-9999-9999-9999-999999999999",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_edited_action_with_no_changes_key_does_not_enqueue():
    """Defensive: a malformed/incomplete edited payload with no `changes` at
    all must never be mistaken for a base retarget."""
    payload = {
        "action": "edited",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_ignored_action_does_not_enqueue():
    payload = {
        "action": "labeled",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_closed_action_cancels_a_pending_ticket(db_query):
    opened = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
    }
    closed = {**opened, "action": "closed"}
    async with await _client() as c:
        await c.post(
            "/webhook", content=json.dumps(opened).encode(),
            headers={
                "X-Hub-Signature-256": _sign(json.dumps(opened).encode()),
                "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
            },
        )
        response = await c.post(
            "/webhook", content=json.dumps(closed).encode(),
            headers={
                "X-Hub-Signature-256": _sign(json.dumps(closed).encode()),
                "X-GitHub-Delivery": "88888888-8888-8888-8888-888888888888",
            },
        )

    assert response.status_code == 202
    assert db_query(
        "SELECT status FROM tickets WHERE repo_full_name = 'owner/repo' AND pr_number = 9"
    ) == [("cancelled",)]


async def test_closed_action_strips_a_live_schedule_notice_footnote(monkeypatch, db_query):
    """A deferred ticket with a posted schedule-notice footnote (notice_not_
    before set) must have that footnote stripped from GitHub when its PR
    closes -- a cancelled ticket is never claimed again, so this is the only
    chance (tickets_needing_notice/clear_notice only ever look at 'deferred'
    tickets, and this one leaves that status on cancel)."""
    ticket_id = store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=11, head_sha="abc123", provider="groq",
        now="2026-01-01T00:00:00+00:00",
    )
    store.set_comment_id(ticket_id, 4242)
    store.mark_notice_posted(ticket_id, "2026-01-02T00:00:00+00:00")

    cleared = []

    def fake_clear(repo_full_name, pr_number, comment_id=None):
        cleared.append((repo_full_name, pr_number, comment_id))
        return None

    monkeypatch.setattr(webhook.github_app, "clear_schedule_notice", fake_clear)

    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 11, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    async with await _client() as c:
        response = await c.post(
            "/webhook", content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Delivery": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )

    assert response.status_code == 202
    assert cleared == [("owner/repo", 11, 4242)]
    assert db_query(
        "SELECT status, notice_not_before FROM tickets "
        "WHERE repo_full_name = 'owner/repo' AND pr_number = 11"
    ) == [("cancelled", None)]


async def test_closed_action_on_untracked_pr_is_a_noop():
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 10, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "99999999-9999-9999-9999-999999999999",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_webhook_ignores_non_target_repo(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/target-repo")
    payload = {"action": "opened",
               "repository": {"full_name": "someone/OTHER-repo"},
               "pull_request": {"number": 5, "head": {"sha": "abc"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)   # existing helper in this module
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-nonmatch"})
    assert resp.status_code == 202                      # accepted, but...
    assert db_query("SELECT count(*) FROM tickets") == [(0,)]   # ...no ticket enqueued


async def test_webhook_accepts_repo_listed_in_comma_separated_allowlist(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo-a,owner/repo-b")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/repo-b"},
               "pull_request": {"number": 9, "head": {"sha": "def"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-multi-match"},
        )
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]


async def test_webhook_rejects_repo_not_in_comma_separated_allowlist(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo-a,owner/repo-b")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/OTHER-repo"},
               "pull_request": {"number": 9, "head": {"sha": "def"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-multi-nonmatch"},
        )
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(0,)]


async def test_webhook_matches_allowlist_entry_case_insensitively(monkeypatch, db_query):
    """GitHub repo names are case-insensitive; a webhook payload's casing need
    not match GITHUB_TARGET_REPO's casing exactly."""
    monkeypatch.setattr(settings, "github_target_repo", "Owner/Repo-A")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/repo-a"},
               "pull_request": {"number": 11, "head": {"sha": "ghi"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-casefold"},
        )
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]


async def test_webhook_accepts_any_repo_when_target_repo_is_star(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "*")
    payload = {"action": "opened",
               "repository": {"full_name": "someone/any-repo"},
               "pull_request": {"number": 3, "head": {"sha": "xyz"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-trackall"})
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]
