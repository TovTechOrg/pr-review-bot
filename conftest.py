"""Shared Postgres test harness. Uses DATABASE_URL if the environment already
provides one (CI's `services: postgres`); otherwise spins a throwaway Postgres
via testcontainers (local dev — Docker required). Never touches Supabase."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from config import settings
from review_queue import store

# Hosts treated as "local/CI Postgres, safe for tests to TRUNCATE". Anything
# else (e.g. a Supabase pooler hostname) is refused unless the operator
# explicitly opts in via ALLOW_REMOTE_TEST_DB=1 -- this guard exists solely so
# an accidentally-exported DATABASE_URL pointing at a real Supabase database
# can never get truncated by a test run.
_LOCAL_TEST_DB_HOSTS = {"localhost", "127.0.0.1"}


def _looks_like_local_test_db(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in _LOCAL_TEST_DB_HOSTS or host.endswith(".internal")


@pytest.fixture(scope="session")
def db_url() -> str:
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        if not _looks_like_local_test_db(env_url) and not os.environ.get(
            "ALLOW_REMOTE_TEST_DB"
        ):
            raise AssertionError(
                "DATABASE_URL does not look like a local/CI Postgres (host must be "
                "'localhost', '127.0.0.1', or end in '.internal'). Refusing to run "
                "destructive tests (TRUNCATE) against it -- this guard protects a real "
                "database (e.g. Supabase) from being wiped by a test run. If this really "
                "is an intentional, disposable local/CI Postgres on an unusual "
                "hostname, set ALLOW_REMOTE_TEST_DB=1 to bypass."
            )
        yield env_url
        # Matches the testcontainers branch below: whichever pool the `db`
        # fixture built against this session's db_url gets closed exactly
        # once, here, rather than per-test -- see `db`'s docstring.
        store.close_pool()
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        # driver=None gives a bare "postgresql://" scheme for raw psycopg3.
        # driver="psycopg" (the previous value) builds a SQLAlchemy-style
        # "postgresql+psycopg://" dialect+driver URL, which psycopg3's own
        # parser cannot read at all ('missing "=" after "postgresql+psycopg:...'"').
        # This was masked until now: CI's services:postgres sets DATABASE_URL
        # directly and never calls this method, and every local run before
        # Docker/WSL integration was enabled failed earlier on
        # docker.errors.DockerException, before this code path ever ran.
        yield pg.get_connection_url(driver=None)
        store.close_pool()


@pytest.fixture
def db(db_url, monkeypatch):
    """Point the store at the test Postgres, ensure schema, and truncate
    between tests. Opt-in (DB-touching test modules request it via an
    autouse wrapper).

    The pool itself is deliberately NOT closed/reopened per test -- only
    initialized once per worker process and reused across every test that
    requests this fixture. store.init_pool() is already idempotent (it only
    builds a fresh ConnectionPool when none exists; otherwise it just
    re-verifies the schema, cheap), so there is never a correctness reason
    to force a rebuild between tests -- db_url never changes within a
    session/worker. Measured via cProfile: the previous close-then-reopen
    cost ~17s cumulative (mostly psycopg_pool's worker-thread shutdown wait)
    across this suite's 234 db-marked tests, over half that group's serial
    runtime, for zero benefit. db_url's fixture (above) closes the pool
    exactly once, at session/worker end.

    Safe even if something else closes the pool mid-session (e.g.
    tests/test_main_lifespan.py's unreachable-Postgres test calls
    store.close_pool() itself, deliberately): init_pool() recreates it on
    the next call regardless of who closed it or why.
    """
    monkeypatch.setattr(settings, "database_url", db_url)
    store.init_pool()
    with store._require_pool().connection() as conn:
        conn.execute("TRUNCATE tickets, runtime_config, slot_config, reviews RESTART IDENTITY")
    yield


@pytest.fixture
def db_exec(db_url):
    """Run a raw statement against the test DB (replaces test-side sqlite3.connect)."""
    import psycopg

    def _exec(sql: str, params: tuple = ()):
        with psycopg.connect(db_url) as conn:
            conn.execute(sql, params)
            conn.commit()

    return _exec


@pytest.fixture
def db_query(db_url):
    """Run a raw query and return the rows (list of tuples)."""
    import psycopg

    def _query(sql: str, params: tuple = ()):
        with psycopg.connect(db_url) as conn:
            return conn.execute(sql, params).fetchall()

    return _query


# Operator-tooling credentials are read only by scripts/deploy.py, and they
# point at REAL infrastructure. A test that forgets to monkeypatch them runs
# against production: during this plan's Task 2 exactly that happened, and a
# live Render service had GITHUB_TARGET_REPO overwritten with a dummy value.
# Later, during Stage 3a's final review, the same gap was found to cover
# GitHub App credentials too: test_checks_registry_matches_what_run_checks_
# actually_runs called run_checks() with no mocking and check_installation_
# and_webhook (github-app) is exactly as capable of reaching live
# infrastructure as render/uptimerobot -- it actually did repoint the real
# GitHub App webhook. Same reasoning as the DATABASE_URL guard above --
# default to inert, and make reaching a live API something a test has to ask
# for by name.
_LIVE_OPERATOR_KEYS = (
    "render_api_key",
    "uptimerobot_api_key",
    "github_app_private_key",
    "github_webhook_secret",
)


@pytest.fixture(autouse=True)
def _quarantine_operator_apis(request, monkeypatch):
    if "live_operator_apis_allowed" in request.fixturenames:
        return
    for name in _LIVE_OPERATOR_KEYS:
        monkeypatch.setattr(settings, name, "")
    # github_app_id is an int field (default 0), unlike the string keys above
    # -- blank it with the type-correct falsy value so check_installation_and_
    # webhook's `if not settings.github_app_id` still degrades to SKIPPED
    # instead of raising on a str-vs-int mismatch.
    monkeypatch.setattr(settings, "github_app_id", 0)
    # settings.database_url defaults to whatever this working copy's real
    # .env points at (a Supabase pooler host in this repo). scripts/deploy.py
    # now opens raw psycopg connections against it (check_database,
    # check_provider, --sync-env's masking guard) and scripts/set_override.py
    # WRITES to it -- a test that forgets to request the `db` fixture must
    # not be able to reach that real database by accident. The `db` fixture
    # (this file) sets settings.database_url explicitly and runs
    # after this autouse fixture within the same test, so it always wins;
    # verified by running the full suite after this change.
    monkeypatch.setattr(settings, "database_url", "")


@pytest.fixture
def live_operator_apis_allowed():
    """Opt out of the quarantine. Requesting this fixture is a deliberate
    statement that the test mocks its own transport (respx) or genuinely
    intends a live call."""
    return True


@pytest.fixture(autouse=True)
def _dashboard_credentials(monkeypatch):
    """A fixed, known-good operator credential for every test. main.py's
    lifespan refuses to boot with any of these empty, and dashboard/auth.py's
    session-token functions need a real value to sign against -- fixed
    literal strings (not e.g. a random token) so tests that assert exact
    credential-check behavior have a known value to check against."""
    monkeypatch.setattr(settings, "dashboard_username", "test-operator")
    monkeypatch.setattr(settings, "dashboard_password", "test-password")
    monkeypatch.setattr(
        settings, "dashboard_session_secret", "test-session-secret-value-for-testing-only"
    )


@pytest.fixture(autouse=True)
def _quarantine_local_slot_discovery(request, monkeypatch):
    """scripts/deploy.py's _wanted_env() reads local .env directly via
    scripts._override.local_slot_values(), bypassing Settings entirely --
    unlike every other value _wanted_env() produces, this one isn't
    automatically hermetic against a contributor's real .env (which may have
    real numbered API-key slots configured). Default to reporting no local
    slots; a test that needs specific slot data monkeypatches
    scripts._override.local_slot_values (or local_slot_indices) itself, which
    naturally overrides this default within that test. Unit tests that
    directly test the real implementation can opt out by requesting the
    'local_slot_discovery_allowed' fixture."""
    if "local_slot_discovery_allowed" in request.fixturenames:
        return
    from scripts import _override

    monkeypatch.setattr(
        _override, "local_slot_values", lambda base, env_path=".env": {}
    )
    monkeypatch.setattr(
        _override, "local_slot_indices", lambda base, env_path=".env": ()
    )


@pytest.fixture
def local_slot_discovery_allowed():
    """Opt out of the quarantine. Requesting this fixture is for unit tests
    that directly test scripts._override.local_slot_values() /
    local_slot_indices() and need the real implementation, not a mock."""
    return True


def _touches_shared_postgres(item: pytest.Item) -> bool:
    """True if item's fixture closure includes db_url -- the root fixture
    that db, db_exec, and db_query all depend on, and that a few tests
    (e.g. tests/test_override_helpers.py) request directly. Checking the
    root rather than the three derived names means a test can't slip
    through by requesting db_url on its own."""
    return "db_url" in item.fixturenames


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-tag every Postgres-touching test with `db` (for `pytest -m "not
    db"` fast iteration) and `xdist_group(name="db")` (so pytest-xdist
    schedules them all onto the same worker, avoiding cross-worker TRUNCATE
    races against the one shared Postgres instance). See the 2026-08-19
    test-suite-performance design doc, section 3c, for why this is keyed off
    db_url specifically.

    `tryfirst=True` is load-bearing, not decoration. `--dist=loadgroup` never
    reads the `xdist_group` marker: pytest-xdist's worker-side
    `WorkerInteractor.pytest_collection_modifyitems` stamps an `@<group>`
    suffix onto `item._nodeid`, and that nodeid *string* is the only thing the
    scheduler groups on. That stamping hookimpl is undecorated, so pluggy
    orders it by registration LIFO -- and this file is an *initial* conftest
    (the repo root, ahead of every directory in `testpaths`), registered
    before `WorkerInteractor`, so
    without `tryfirst` xdist stamps first, while no item carries the marker
    yet, and every db test ends up its own singleton group spread across every
    worker (each spinning its own testcontainers Postgres). The failure is
    silent -- all tests still pass and `-m db` still selects correctly, since
    marker selection is evaluated after both hooks have run.
    `tests/test_xdist_group_ordering.py` is the regression guard."""
    for item in items:
        if _touches_shared_postgres(item):
            item.add_marker(pytest.mark.db)
            item.add_marker(pytest.mark.xdist_group(name="db"))
