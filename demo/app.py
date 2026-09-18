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
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import github_app as real_github_app
import render_client as real_render_client
from fastapi import Response
from providers import catalog as real_catalog
from providers import credentials as real_credentials
from providers import vertex_credentials as real_vertex_credentials
from review_queue import store as real_store

from config import settings

from demo import github_app as demo_github_app
from demo import providers_mock as demo_providers_mock
from demo import render_client as demo_render_client
from demo import session as demo_session
from demo import store as demo_store
from demo.provider import MockProvider, demo_provider_and_model

logger = logging.getLogger(__name__)

_MOCKED_GITHUB = (
    "fetch_pr_diff", "upsert_comment", "append_review_footnote",
    "append_schedule_notice", "clear_schedule_notice", "react_eyes_to_pr",
    "discover_and_verify_installation_id",
    # dashboard/environment.py's github_app guided-setup Validate calls these
    # two directly on the module object -- without this rebinding it makes a
    # real GitHub App JWT/installation-discovery call on a service that has
    # no real GitHub App credential.
    "_app_jwt_client_for", "discover_installation_id_for_app",
)

# dashboard/environment.py calls all five on the module object. Without this
# rebinding the Environment panel makes a real HTTPS call to api.render.com
# with no API key on a service that deliberately has none -- a 401 and a 500
# on GET /api/environment/render, on a page whose whole premise is that no
# external call can fail it.
_MOCKED_RENDER = (
    "find_service_id", "env_vars", "push_env_var", "delete_env_var", "trigger_deploy",
)

# dashboard/environment.py's Guided-setup Validate/Apply and the config
# table's per-row Validate all call these on the module objects too. Without
# this rebinding, the Environment tab's credential-validate/model-catalog
# calls either 401 against a real provider with a typed-in demo value, or
# (for the config table's slot-based lookups) short-circuit on
# "no_credential_configured" since the demo service's real *_API_KEY/
# VERTEX_GCP_SERVICE_ACCOUNT_KEY env vars are unset -- either way, empty
# model dropdowns on a page whose premise is that nothing here fails.
_MOCKED_CATALOG = (
    "list_gemini_models", "list_groq_models", "list_vertex_models",
    "probe_vertex_model", "probe_gemini_model", "list_accessible_projects",
)
_MOCKED_CREDENTIALS = ("resolve",)
_MOCKED_VERTEX_CREDENTIALS = ("resolve_service_account_info",)

# How often idle sessions (and their tickets/reviews/comments) are evicted.
SWEEP_INTERVAL_SECONDS = 300.0


def install_mocks() -> None:
    for name in _MOCKED_GITHUB:
        setattr(real_github_app, name, getattr(demo_github_app, name))

    for name in _MOCKED_RENDER:
        setattr(real_render_client, name, getattr(demo_render_client, name))

    for name in _MOCKED_CATALOG:
        setattr(real_catalog, name, getattr(demo_providers_mock, name))
    for name in _MOCKED_CREDENTIALS:
        setattr(real_credentials, name, getattr(demo_providers_mock, name))
    for name in _MOCKED_VERTEX_CREDENTIALS:
        setattr(real_vertex_credentials, name, getattr(demo_providers_mock, name))

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
    from providers import active_model

    def _demo_provider() -> MockProvider:
        # active_model's cache is refreshed once per claimed ticket by
        # review_queue/dispatcher.py::_refresh_slot_config, BEFORE any
        # specialist (and therefore this function) runs -- so by the time
        # this is called it already reflects the ticket-scoped model
        # demo/store.py::get_all_slot_configs resolved (the reader's own
        # pick, or the priced default when none was requested/valid).
        provider = real_store.get_provider_override() or "groq"
        model = active_model.active_model(provider, 0)
        provider, model = demo_provider_and_model(provider, model)
        return MockProvider(provider=provider, model=model)

    specialists.base.get_provider = _demo_provider


install_mocks()

from main import app  # noqa: E402  (must follow install_mocks)

__all__ = ["app", "install_mocks"]

from demo.routes import router as demo_router  # noqa: E402

app.include_router(demo_router)

# Overrides nothing in main.py -- `/demo-static/demo.js` is a path main.py
# never mounts (its own static mount lives at `/static/fonts`, see main.py's
# `app.mount("/static/fonts", ...)`), so this can't collide with it. Serves
# demo/static/demo.js (banner injection, readonly login prefill, bootstrap
# fetch, and the post-review CTA), which login.html/dashboard.html load via a
# <script> tag. Rendered (not a plain StaticFiles mount) so the CTA's wizard
# and guide links come from `settings` instead of being baked into the file on
# disk -- read and templated once at import time (not per-request: settings
# never change over a process's lifetime).
#
# json.dumps, not an f-string, for the substituted value: it's dropped in as a
# bare token (demo.js has `__REAL_WIZARD_URL__` unquoted, not `"...""), so
# json.dumps supplies both the surrounding quotes and correct escaping -- a
# plain f-string would let a `"` or `\` in an operator-set REAL_WIZARD_URL/
# GUIDE_BASE_URL override break out of the string literal.
_DEMO_JS_TEMPLATE = (Path(__file__).parent / "static" / "demo.js").read_text(encoding="utf-8")
_DEMO_JS_RENDERED = _DEMO_JS_TEMPLATE.replace(
    "__REAL_WIZARD_URL__", json.dumps(settings.real_wizard_url)
).replace("__GUIDE_URL__", json.dumps(f"{settings.guide_base_url}/setup/"))


async def _demo_js() -> Response:
    # text/javascript (not application/javascript): demo.js has Hebrew
    # strings, and Starlette only auto-appends `; charset=utf-8` for a
    # text/* media type -- the old StaticFiles mount served exactly this
    # content-type, so this keeps decoding correct.
    return Response(content=_DEMO_JS_RENDERED, media_type="text/javascript")


def register_demo_static_route(target_app) -> None:
    """Exists so tests/test_demo_*.py's `demo_chrome_installed` fixtures can
    re-apply this route onto the shared `main.app` singleton per-test (see
    their docstrings: conftest.py's autouse restore fixture strips it back
    off after every test, and a plain module import is a no-op cache hit the
    second time) without each duplicating this registration inline."""
    target_app.add_api_route("/demo-static/demo.js", _demo_js, methods=["GET"], name="demo-static")


register_demo_static_route(app)


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


# The launcher page (guide/demo/index.html, served from GitHub Pages) polls
# this service's demo_launcher_ping_path (demo/routes.py's launcher_ping) to
# know when a cold start has finished. That is a cross-origin read, so the
# response needs an explicit allow header or the browser hands the launcher
# an opaque failure indistinguishable from "still booting" -- see the
# 2026-09-16 design's "The launcher" section. NOT scoped to "/healthz"
# (config.py's demo_launcher_ping_path field comment has the full reason: an
# ad-blocker/privacy extension's filter list blocked that literal path
# client-side, 2026-09-17).
#
# Scoped to demo_launcher_ping_path by path, with no
# Access-Control-Allow-Credentials, so no cookie ever rides on it and no
# other demo route becomes readable from another origin. No `Vary: Origin`:
# the value is a constant that does not depend on the request's own Origin,
# so there is nothing for a cache to vary on.
_guide_parts = urlsplit(settings.guide_base_url)
LAUNCHER_ORIGIN = f"{_guide_parts.scheme}://{_guide_parts.netloc}"


@app.middleware("http")
async def _allow_launcher_health_polling(request, call_next):
    response = await call_next(request)
    if request.url.path == settings.demo_launcher_ping_path:
        response.headers["access-control-allow-origin"] = LAUNCHER_ORIGIN
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
