import inspect

from review_queue import store as real_store
from demo import store as demo_store


def _public(module) -> set[str]:
    return {
        name for name, obj in vars(module).items()
        if not name.startswith("_") and inspect.isfunction(obj)
        and obj.__module__ == module.__name__
    }


def test_mock_covers_the_real_store_public_surface():
    """The demo breaks silently when the real store grows a function.

    This is the guard: the failure would otherwise surface to a stranger
    following a link from an article, months after the change.
    """
    missing = _public(real_store) - _public(demo_store)
    assert not missing, f"demo/store.py is missing: {sorted(missing)}"


def test_enqueue_then_claim_returns_the_ticket():
    demo_store.reset()
    demo_store.enqueue_or_update(
        repo_full_name="bot-demo/example-app", pr_number=7,
        head_sha="abc", provider="groq", now="2026-09-16T00:00:00+00:00",
    )
    ticket = demo_store.claim_next_due("2026-09-16T00:00:01+00:00")
    assert ticket is not None
    assert ticket.pr_number == 7
    assert demo_store.claim_next_due("2026-09-16T00:00:02+00:00") is None


def test_boot_config_getters_are_valid():
    demo_store.reset()
    assert demo_store.get_provider_override() in {"gemini", "vertex", "groq"}
    provider = demo_store.get_provider_override()
    assert demo_store.get_slot_config(provider, 0) is not None
    assert all(v is not None for v in demo_store.get_cooldown_overrides())
    assert demo_store.get_dispatcher_tuning_config()
