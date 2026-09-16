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

from pathlib import Path

import github_app as real_github_app
from fastapi.staticfiles import StaticFiles
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

    # demo_store.install() rebinds every public review_queue.store function
    # onto the real module -- EXCEPT its own `_NEVER_REBIND` set
    # (effective_cooldown/next_cooldown_level/usage_bucket_start), which are
    # thin passthrough wrappers that call `real_store.<name>(...)` internally.
    # Reimplementing this loop here instead of calling install() (as an
    # earlier version of this file did) rebinds those three too, pointing
    # real_store.<name> at a wrapper whose own body calls real_store.<name> --
    # infinite recursion the instant any of them runs (e.g. dispatcher.py's
    # finalize path calling store.effective_cooldown()).
    demo_store.install()

    import specialists.base

    def _demo_provider() -> MockProvider:
        provider = real_store.get_provider_override() or "groq"
        provider, model = demo_provider_and_model(provider)
        return MockProvider(provider=provider, model=model)

    specialists.base.get_provider = _demo_provider


install_mocks()

from main import app  # noqa: E402  (must follow install_mocks)

__all__ = ["app", "install_mocks"]

from demo.routes import router as demo_router  # noqa: E402

app.include_router(demo_router)

# Overrides nothing in main.py -- `/demo-static` is a path main.py never
# mounts (its own static mount lives at `/static/fonts`, see main.py's
# `app.mount("/static/fonts", ...)`), so this can't collide with it. Serves
# demo/static/demo.js (banner injection, readonly login prefill, bootstrap
# fetch), which login.html/dashboard.html load via a <script> tag.
app.mount(
    "/demo-static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="demo-static",
)


@app.middleware("http")
async def _cookieless_bypasses_login(request, call_next):
    """The dashboard session IS a cookie, so a reader who cannot store one
    could never pass the login form no matter the credentials. Safe only
    because the demo guards nothing -- every value behind the gate is
    synthetic. Never a pattern for the real dashboard.
    """
    from dashboard.auth import SESSION_COOKIE_NAME, create_session_token

    if SESSION_COOKIE_NAME not in request.cookies:
        request.scope.setdefault("headers", [])
        token = create_session_token(remember=False)
        request.scope["headers"] = [
            *request.scope["headers"],
            (b"cookie", f"{SESSION_COOKIE_NAME}={token}".encode()),
        ]
    return await call_next(request)
