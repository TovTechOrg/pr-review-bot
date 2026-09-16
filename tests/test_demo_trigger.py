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
    assert evicted == 1
    assert session.is_known("fresh") is True
    assert session.is_known("stale") is False
