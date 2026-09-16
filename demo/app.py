"""Demo entrypoint: `uvicorn demo.app:app`.

Rebinds every external boundary to an in-memory mock BEFORE importing main,
then re-exports main's app unchanged. main.py is never edited and the real
image never contains this package, so there is no runtime flag that could
turn demo behaviour on in production.

Attribute rebinding works because callers look these up at call time
(`store.claim_next_due(...)`). specialists.base is the exception: it does
`from providers.factory import get_provider`, binding the name at import
time, so the name to patch is `specialists.base.get_provider` -- the same one
the existing tests patch.
"""

from __future__ import annotations

import github_app as real_github_app
from review_queue import store as real_store

from demo import github_app as demo_github_app
from demo import store as demo_store
from demo.provider import MockProvider, demo_provider_and_model

_MOCKED_GITHUB = (
    "fetch_pr_diff", "upsert_comment", "append_review_footnote",
    "append_schedule_notice", "clear_schedule_notice", "react_eyes_to_pr",
    "discover_and_verify_installation_id",
)


def install_mocks() -> None:
    for name in _MOCKED_GITHUB:
        setattr(real_github_app, name, getattr(demo_github_app, name))

    for name in dir(demo_store):
        if not name.startswith("_") and hasattr(real_store, name):
            setattr(real_store, name, getattr(demo_store, name))

    import specialists.base

    def _demo_provider() -> MockProvider:
        provider = real_store.get_provider_override() or "groq"
        provider, model = demo_provider_and_model(provider)
        return MockProvider(provider=provider, model=model)

    specialists.base.get_provider = _demo_provider


install_mocks()

from main import app  # noqa: E402  (must follow install_mocks)

__all__ = ["app", "install_mocks"]
