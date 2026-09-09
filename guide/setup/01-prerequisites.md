# Step 1: Install prerequisites

The goal of this step is to get the checkout running and its test suite
green before touching any credentials — that proves the toolchain works
before anything harder is layered on top. Get everything below in place
*first*, then run the test suite at the end of this page — running it any
earlier just means hitting an avoidable failure.

## What you need

| Tool | Why | Check |
|---|---|---|
| **Python 3.12** | pinned in `.python-version` | see below |
| [**uv**](https://docs.astral.sh/uv/) | this project's package/venv manager | `uv --version` |
| **git** | to clone the repo | `git --version` |
| **Docker, *or* a local Postgres** | the test suite needs a real, disposable Postgres | see below |
| [**`gh`**](https://cli.github.com/) | needed later to open the live demo PR (Step 8) — install and authenticate it now, since it's a one-time setup step like everything else on this page | `gh --version` |

Checking your Python version:

=== "Linux"

    ```bash
    python3 --version
    ```

=== "macOS"

    ```bash
    python3 --version
    ```

=== "Windows"

    ```powershell
    py --version
    ```

The Docker-or-local-Postgres line is one conditional, not two separate
requirements: `tests/conftest.py`'s `db_url` fixture spins up a throwaway
Postgres 16 container via `testcontainers` automatically when Docker is
present, or reuses a `DATABASE_URL` **environment variable** you already
have exported. Without *either* one, the DB-touching tests fail with an
opaque testcontainers error that doesn't say "install Docker" — so get one
of the two in place before running the test suite below.

This is a real, exported shell variable, not a line written into `.env` —
the test suite reads `os.environ` directly and never loads `.env` (that
loading is `Settings`' own mechanism, used by the app itself from Step 2
onward, not by the test harness).

=== "bash"

    ```bash
    export DATABASE_URL=postgresql://postgres:<password>@localhost:5432/postgres
    uv run pytest
    ```

=== "PowerShell"

    ```powershell
    $env:DATABASE_URL = "postgresql://postgres:<password>@localhost:5432/postgres"
    uv run pytest
    ```

!!! warning "This DATABASE_URL must be local — not Supabase or any other remote Postgres"
    The test suite `TRUNCATE`s tables between tests, so `tests/conftest.py`
    refuses to run against any `DATABASE_URL` whose host isn't `localhost`,
    `127.0.0.1`, or `.internal` (CI's own Postgres) — a remote host like a
    Supabase pooler hits an immediate `AssertionError`, not the opaque
    testcontainers error. That error names an escape hatch,
    `ALLOW_REMOTE_TEST_DB=1`, but **don't reach for it here**: setting it
    would let every future test run truncate whatever real database
    `DATABASE_URL` points at — a real problem once that's also the Supabase
    project you point the app at for real in Step 4. If you don't want
    Docker, install Postgres natively instead (see below) rather than
    pointing this at a hosted service.

## Installing Docker

Skip this if you already have Postgres reachable at `localhost` some other
way (see the native install below) — Docker is only one way to satisfy
that prerequisite, not the requirement itself.

=== "Linux"

    ```bash
    sudo apt install docker.io
    ```

    Then add yourself to the `docker` group. Official install page:
    <https://docs.docker.com/get-docker/>

=== "macOS"

    ```bash
    brew install --cask docker
    ```

    Official install page: <https://docs.docker.com/get-docker/>

=== "Windows"

    ```powershell
    winget install Docker.DockerDesktop
    ```

    Official install page: <https://docs.docker.com/get-docker/>

## Installing Postgres natively (no Docker)

If you'd rather not run Docker at all, install Postgres 16 directly from
<https://www.postgresql.org/download/>, create a database, then export
`DATABASE_URL` in your shell before running the test suite the same way
shown above — a `localhost` host is what makes this count as local for the
warning above.

## Installing and authenticating `gh`

=== "Linux"

    See <https://github.com/cli/cli/blob/trunk/docs/install_linux.md> — most
    distros install it from a package repo (e.g. `sudo apt install gh` once
    that repo is added).

=== "macOS"

    ```bash
    brew install gh
    ```

=== "Windows"

    ```powershell
    winget install GitHub.cli
    ```

Then authenticate it:

```bash
gh auth login
```

This walks you through a browser-based login and picks up wherever you're
already logged into GitHub in your default browser.

!!! warning "Use the same GitHub account everywhere in this guide"
    Step 2 (create the App) and Step 3 (install the App) both happen in a
    **browser** — whatever account that browser session is logged into ends
    up owning and hosting the App. `gh auth login` separately authenticates
    **this machine's `gh` CLI**, which Step 8 uses to push a branch and open
    a real PR. Nothing connects the two: if you have more than one GitHub
    account (e.g. personal + work), it's easy to end up with the App on one
    account and `gh` authenticated as another, and end up with a repo `gh`
    can't push to or an installation that doesn't cover it.

    Avoid the whole problem by using **one account** for `gh auth login`
    here, approving the App in Steps 2–3, and owning the repo you'll later
    set `GITHUB_TARGET_REPO` to. If you're ever unsure which account is
    currently active, `gh auth status` names it. `uv run python -m
    scripts.doctor`'s `gh-auth` and `target-repo` rows also catch a mismatch
    directly, once `GITHUB_TARGET_REPO` is set in Step 8 — but it's simpler
    to just not create one.

## Get the checkout running

With Python, uv, git, and Docker (or a local Postgres exported as
`DATABASE_URL`) all in place:

```bash
git clone <your-fork-or-clone-url>   # e.g. https://github.com/<you>/pr-review-bot.git
cd pr-review-bot   # or whatever your clone created
uv sync
uv run pytest
```

If you're not using Docker, export `DATABASE_URL` first as shown earlier on
this page, then run `uv run pytest`.

`<your-fork-or-clone-url>` is whatever URL you're getting this project's
source from — your own fork if you plan to push changes anywhere, or a
direct clone of the upstream repo otherwise; either works equally well for
getting the checkout running. Step 6 has one more wrinkle:
Render needs an actual Git repo URL to build from, and the upstream one
works too, with tradeoffs — see that step for the options.

If `uv run pytest` passes, the checkout is sound and every tool above is
correctly in place.

## Next

```bash
uv run python -m scripts.doctor
```

Run it now — it checks every prerequisite above (and everything in the
steps that follow) and tells you exactly what's still missing.
