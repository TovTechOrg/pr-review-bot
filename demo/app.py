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

import asyncio
import contextlib
import logging
from pathlib import Path

import github_app as real_github_app
import render_client as real_render_client
from fastapi.staticfiles import StaticFiles
from review_queue import store as real_store

from demo import github_app as demo_github_app
from demo import render_client as demo_render_client
from demo import session as demo_session
from demo import store as demo_store
from demo.provider import MockProvider, demo_provider_and_model

logger = logging.getLogger(__name__)

_MOCKED_GITHUB = (
    "fetch_pr_diff", "upsert_comment", "append_review_footnote",
    "append_schedule_notice", "clear_schedule_notice", "react_eyes_to_pr",
    "discover_and_verify_installation_id",
)

# dashboard/environment.py calls all five on the module object. Without this
# rebinding the Environment panel makes a real HTTPS call to api.render.com
# with no API key on a service that deliberately has none -- a 401 and a 500
# on GET /api/environment/render, on a page whose whole premise is that no
# external call can fail it.
_MOCKED_RENDER = (
    "find_service_id", "env_vars", "push_env_var", "delete_env_var", "trigger_deploy",
)

# How often idle sessions (and their tickets/reviews/comments) are evicted.
SWEEP_INTERVAL_SECONDS = 300.0


def install_mocks() -> None:
    for name in _MOCKED_GITHUB:
        setattr(real_github_app, name, getattr(demo_github_app, name))

    for name in _MOCKED_RENDER:
        setattr(real_render_client, name, getattr(demo_render_client, name))

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


async def _demo_session_required(request, exc):
    """main.py's `_handle_session_required`, with the query string kept.

    The demo's documented entry point is `GET /?provider=vertex` -- the
    wizard's "Finish & Deploy" redirect target -- reached by a reader who is
    not logged in yet. main.py's handler answers that with a bare
    `RedirectResponse("/login")`, dropping the provider choice one hop
    BEFORE login.html's own (already query-preserving) post-login redirect
    ever runs, so the review silently reports the default provider instead
    of the one the reader picked.

    Registered here rather than fixed in main.py because main.py is the real,
    production dashboard's code and is shared with the non-demo service.
    Starlette resolves an exception handler through `app.exception_handlers`,
    a plain dict keyed by exception class, so a later `add_exception_handler`
    for the same class replaces the earlier registration outright -- the same
    post-hoc override shape this module already uses for the router, the
    middleware, the static mount and the lifespan.

    The `/api/` branch is byte-for-byte main.py's, deliberately: only the
    redirect branch is being changed here.
    """
    from fastapi.responses import JSONResponse, RedirectResponse

    if request.url.path.startswith("/api/"):
        return JSONResponse({"valid": False, "reason": "unauthenticated"}, status_code=401)
    query = request.url.query
    return RedirectResponse("/login" + (f"?{query}" if query else ""), status_code=303)


def install_session_redirect_override() -> None:
    """Idempotent, and callable by tests after conftest.py restores app state."""
    from dashboard.auth import SessionRequired

    app.add_exception_handler(SessionRequired, _demo_session_required)
    # Starlette caches the built ExceptionMiddleware in `middleware_stack` on
    # first use; without this the restored/stale stack keeps main.py's
    # handler for the life of the process.
    app.middleware_stack = None


install_session_redirect_override()


# A non-secret marker, readable by JavaScript on purpose: demo/static/demo.js
# checks for it on the login page to find out whether this browser kept the
# cookie it was just handed. Not `secure`, so it also survives the plain-HTTP
# transport the test suite uses.
PROBE_COOKIE_NAME = "demo_cookie_probe"
PROBE_MAX_AGE_SECONDS = 60 * 60
# Set by demo.js on the redirect it issues once it has PROVEN cookies do not
# stick. Honoured only for a request that carries no cookies at all, so it
# cannot short-circuit login for a browser that simply has not logged in yet.
COOKIELESS_QUERY_PARAM = "cookieless"


def _cookie_hostile(request) -> bool:
    """True only for a visitor PROVEN unable to store cookies.

    The first half (`not request.cookies`) is necessary and nowhere near
    sufficient: a normal browser's very first request ever also carries no
    cookies, and bypassing on that alone meant the login screen -- an
    explicit step in the design's reader path, running the real auth check --
    never rendered for anyone, on any browser, ever. It also minted a fresh
    session JWT per request, which for a genuinely cookie-hostile visitor
    changed their identity on every single request, breaking webhook.py's
    delivery dedup and growing demo/session.py's `_last_seen` unbounded.

    The second half is the actual proof: every response below hands out a
    plain, JS-readable probe cookie, and demo/static/demo.js only adds this
    query parameter after reloading and finding that cookie absent.
    """
    return (
        not request.cookies
        and request.query_params.get(COOKIELESS_QUERY_PARAM) == "1"
    )


@app.middleware("http")
async def _demo_request_context(request, call_next):
    """Establish the visitor's demo identity, and let a cookie-hostile one in.

    The dashboard session IS a cookie, so a reader who cannot store one could
    never pass the login form no matter the credentials. Safe only because
    the demo guards nothing -- every value behind the gate is synthetic.
    Never a pattern for the real dashboard.
    """
    from dashboard.auth import SESSION_COOKIE_NAME, create_session_token

    cookies = dict(request.cookies)
    bypass = _cookie_hostile(request)
    if bypass:
        request.scope.setdefault("headers", [])
        token = create_session_token(remember=False)
        request.scope["headers"] = [
            *request.scope["headers"],
            (b"cookie", f"{SESSION_COOKIE_NAME}={token}".encode()),
        ]

    # Never the freshly-minted token above: that changes on every request.
    # A cookie-hostile visitor gets one stable shared identity, which is
    # exactly the "stateless shared view" the design calls for.
    demo_session_id = cookies.get(SESSION_COOKIE_NAME) or (
        demo_session.SHARED_SESSION_ID if bypass else None
    )
    # `or current_session.get()` covers the in-process self-delivery:
    # demo/trigger.py POSTs to this same app over an ASGI transport from
    # inside the bootstrap request, so that nested request passes through
    # here too -- carrying no cookies of its own. Without this it would reset
    # the identity to None right before enqueue_or_update reads it.
    demo_session_id = demo_session_id or demo_session.current_session.get()

    if demo_session_id is not None:
        demo_session.touch(demo_session_id)
    context_token = demo_session.current_session.set(demo_session_id)
    try:
        response = await call_next(request)
    finally:
        demo_session.current_session.reset(context_token)

    if PROBE_COOKIE_NAME not in cookies:
        response.set_cookie(
            PROBE_COOKIE_NAME,
            "1",
            max_age=PROBE_MAX_AGE_SECONDS,
            httponly=False,
            samesite="lax",
        )
    return response


async def sweep_once() -> int:
    """Evict idle sessions and everything they own. Returns rows dropped."""
    evicted = demo_session.sweep()
    dropped = demo_store.evict_sessions(evicted)
    demo_github_app.trim_comments()
    if evicted:
        logger.info("demo sweep: evicted %d sessions, %d rows", len(evicted), dropped)
    return dropped


async def _sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a sweep failure must never kill the loop
            logger.exception("demo sweep failed")


_main_lifespan_context = app.router.lifespan_context


@contextlib.asynccontextmanager
async def _demo_lifespan(app):
    """main.py's own lifespan, wrapped -- not replaced, and main.py untouched.

    Starlette ignores `on_startup`/`on_shutdown` handlers entirely once a
    lifespan context manager is supplied, so wrapping the existing one is the
    only way to attach the demo's eviction task without editing main.py.
    """
    async with _main_lifespan_context(app):
        task = asyncio.create_task(_sweep_forever())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app.router.lifespan_context = _demo_lifespan
