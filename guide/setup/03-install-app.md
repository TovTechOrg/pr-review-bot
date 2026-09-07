# Step 3: Install the App on your repo(s)

This step only happens in a browser — GitHub does not permit an App to
install itself, so there's no CLI or script for it.

## Pick a repo before you install

Whichever repo(s) you install the App on below is also what Step 8 — the
last step — uses to open a real demo PR. Decide that now,
rather than discovering the gap when that step's `seed_demo_pr` fails
because no repo was ever set. If you don't already have one you're happy
pushing throwaway commits to, create one (needs `gh` from Step 1,
authenticated as the account you're about to install the App on — see that
step's warning about keeping one account consistent through this guide):

```bash
gh repo create <you>/pr-review-bot-demo --private --clone=false --add-readme
```

`--add-readme` matters here, not just cosmetic: without an initial commit a
fresh repo has no default branch yet, and Step 8's `seed_demo_pr` pushes a
new branch and asks `gh pr create` to open a PR against that (nonexistent)
default branch — which fails with a cryptic `GraphQL: can't be blank
(createPullRequest)` instead of a clear "no such branch" error. The README
commit is what gives the repo a real default branch to open the demo PR
against.

## Install it

1. Go to `https://github.com/settings/apps/<your-app-slug>`.
2. Click **Install App**.
3. Choose **All repositories** (covers the repo above automatically), or
   select specific repos and make sure the one above is included.

Whichever GitHub account this browser session installs the App on is the
account that matters from here on — including for Step 8's demo PR, which
needs `gh` (Step 1) authenticated as *that same* account. Step 1's warning
about this covers what goes wrong when they don't match and how `doctor`
catches it if they don't.

## Set `GITHUB_APP_INSTALLATION_ID`

**Required** — never auto-discovered or guessed on your behalf; the service
refuses to start without it, and re-verifies it against the App's actual
installation on every boot, so a value that's gone stale (e.g. the App was
uninstalled and reinstalled) fails loudly rather than silently drifting.

You don't have to hunt it down by hand: with `GITHUB_APP_INSTALLATION_ID`
still blank, run

```bash
uv run python -m scripts.doctor
```

not `scripts.deploy` — this early, before a public URL exists (that's Step
6), `deploy` refuses to run at all (`a public base URL
(PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) is required`, exit 2) before it ever
reaches an installation-discovery check. `doctor` needs no public URL for
this: its `github-install` row calls the same GitHub API discovery and
prints `installation=<id>` on success, using only the App credentials you
already set in Step 2.

!!! warning "doctor only prints this value -- it does not save it for you"
    `doctor` is [read-only and never writes a file](../index.md) — the id it
    prints is gone the moment your terminal scrolls past it unless you act
    on it.
    Copy that number into `.env` yourself as `GITHUB_APP_INSTALLATION_ID`
    before moving on. Skip this and every later `doctor`/`deploy` run keeps
    reporting it as missing, and the service refuses to boot outright once
    you reach Step 8.

## Set `GITHUB_TARGET_REPO` to that same repo

Choosing "All repositories" above decides which repos the *installation*
covers — which repos the App can see at all. `GITHUB_TARGET_REPO` is a
separate, **required** setting on top of that: an allowlist that further
narrows which of the *installed* repos the bot actually acts on. It doesn't
change what the App can see, only what it responds to.

Setting it to `*` means the bot acts on **every** repo the installation
covers. That's only a safe choice because the App is private (step 2) —
only accounts you chose could install it in the first place. If the App
were public, `GITHUB_TARGET_REPO=*` would mean accepting events from any
repo any third party chose to install it on. The service refuses to boot at
all if this is left unset, precisely so it's never accidentally left in
either state.

Set it now, in `.env.config`, to the repo you just installed on:

```
GITHUB_TARGET_REPO=<you>/pr-review-bot-demo
```

Step 8 needs a concrete answer to "which repo does the demo PR
go on", and `doctor`'s `target-repo` row and `scripts/seed_demo_pr` both read
this exact same setting — so setting it here, once, means Step 8 needs no
further setup, and re-running `doctor` from this point on actually exercises
the `gh-auth`/`target-repo` checks instead of leaving them `SKIPPED`. You can
always switch it to `GITHUB_TARGET_REPO=*` later to move the deployed bot to
track-all mode.

## Next

Continue to [Step 4: configure an LLM provider](04-llm-provider.md).
