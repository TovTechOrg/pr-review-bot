# Step 6: Create the Render service

## Point Render at a repo

Render's Blueprint flow needs a Git repository to build `render.yaml` from.
Three ways to give it one:

- **The upstream repo's own URL, unchanged** — Render can build straight
  from a public repo URL with no fork and no push access, auto-detecting
  `render.yaml` (or a different blueprint file, if you point it at one). The
  simplest option if all you want is to run the bot as this guide documents
  it. The tradeoff: which commits trigger a redeploy now follows the
  upstream project's own history, not yours — fine here, not if you plan to
  modify the code and want your own pushes to redeploy it.
- **Fork it on GitHub** — your own copy, with auto-deploy wired to your own
  pushes. The standard choice if you expect to change anything.
- **Push your local clone (Step 1) to a new repo you own** — the same
  benefit as forking, useful if you already made local changes before ever
  pushing anywhere.

## Get a Render API key

Get one now, before creating the service below — `scripts/deploy.py
--sync-env` (next step) and `doctor`/`deploy`'s live checks all need a
`RENDER_API_KEY` to act on your behalf. Get one from the Render dashboard →
**Account Settings → API Keys**, then set it as `RENDER_API_KEY` locally in
`.env`. It's operator-local tooling, never something the service itself
sees.

!!! warning "RENDER_API_KEY is not a service env var"
    Never add it to `render.yaml` and never give it to the Render service
    itself.

## Create it

1. Go to <https://render.com/dashboard>.
2. Click **New +** → **Blueprint** → point it at the repo you picked above.

`render.yaml` declares `runtime: docker` with a `dockerfilePath`, so Render
builds and runs this project's `Dockerfile` as-is — there is no separate
Build/Start command to configure; the container's entrypoint is the
Dockerfile's own `CMD`
(`uv run --no-dev uvicorn main:app --host 0.0.0.0 --port 8000`).

Every var `render.yaml` declares is marked `sync: false`, so Render's
Blueprint form offers a box for each one — **leave all of them blank**.
Nothing needs to be typed here by hand: Step 7's `--sync-env` pushes every
one of them, correctly, in one shot, right after this — including the ones
that hand-copying into a web form is easiest to get wrong, like
`GITHUB_APP_PRIVATE_KEY`'s base64 blob.

!!! note "Docs-only pushes never trigger a deploy"
    `render.yaml` sets `buildFilter.ignoredPaths: ["**/*.md"]`, so a push
    that only touches a Markdown file never triggers a build/deploy — the
    Dockerfile never copies any of it into the image.

## Expect this first deploy to fail — that's fine

Click **Deploy**. With every var left blank, the container starts and
immediately exits: `main.py`'s startup refuses to run with no
`GITHUB_WEBHOOK_SECRET`, no `DATABASE_URL`, and so on — deliberately, so
a missing credential fails loudly rather than silently limping along. Render
will show this deploy as failed, and may show it retrying and failing again.
Leave it — there's nothing to fix here, it's expected with every var still
blank. Step 7 resolves it in one command.

## Next

Continue to [Step 7: sync config and verify](07-sync.md).
