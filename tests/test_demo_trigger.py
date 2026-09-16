import hashlib
import hmac
import json

from demo.content import DEMO_PR_NUMBER, DEMO_REPO


def test_signed_self_delivery_is_accepted_and_deduped(monkeypatch):
    from demo import trigger

    body = json.dumps(trigger.build_payload()).encode()
    signature = "sha256=" + hmac.new(b"demo-webhook-secret", body, hashlib.sha256).hexdigest()

    from hmac_verify import verify_signature
    assert verify_signature(body, signature, "demo-webhook-secret")


def test_delivery_id_is_stable_per_session():
    from demo import trigger

    first = trigger.delivery_id_for("session-abc")
    again = trigger.delivery_id_for("session-abc")
    other = trigger.delivery_id_for("session-xyz")
    assert first == again
    assert first != other


def test_payload_names_the_demo_pr():
    from demo import trigger

    payload = trigger.build_payload()
    assert payload["action"] == "opened"
    assert payload["repository"]["full_name"] == DEMO_REPO
    assert payload["pull_request"]["number"] == DEMO_PR_NUMBER
    assert payload["pull_request"]["head"]["sha"]


def test_sweep_evicts_only_idle_sessions():
    from demo import session

    session.reset()
    session.touch("fresh", now=1_000.0)
    session.touch("stale", now=0.0)
    evicted = session.sweep(now=1_000.0 + session.TTL_SECONDS)
    # The ids, not just a count: demo/app.py's sweep task needs them to drop
    # those sessions' tickets and reviews from demo/store.py too.
    assert evicted == ["stale"]
    assert session.is_known("fresh") is True
    assert session.is_known("stale") is False


def test_each_session_reviews_its_own_pull_request():
    """review_queue/store.py dedups on (repo, pr_number). With one fixed
    number every concurrent visitor collapsed onto a single shared ticket,
    so only one of them could ever have a review at all."""
    from demo import trigger
    from demo.session import SHARED_SESSION_ID

    assert trigger.pr_number_for("session-abc") == trigger.pr_number_for("session-abc")
    assert trigger.pr_number_for("session-abc") != trigger.pr_number_for("session-xyz")
    # The canonical number is kept for the one shared, cookie-hostile view --
    # the one the docs and the article's screenshots describe.
    assert trigger.pr_number_for(None) == DEMO_PR_NUMBER
    assert trigger.pr_number_for(SHARED_SESSION_ID) == DEMO_PR_NUMBER
    assert trigger.build_payload("session-abc")["pull_request"]["number"] != DEMO_PR_NUMBER


def test_comments_are_capped_rather_than_growing_forever():
    from demo import github_app as demo_github_app

    demo_github_app.reset()
    for i in range(5):
        demo_github_app.upsert_comment("bot-demo/example-app", i, "body")
    assert len(demo_github_app._comments) == 5
    demo_github_app.trim_comments(limit=2)
    assert len(demo_github_app._comments) == 2
