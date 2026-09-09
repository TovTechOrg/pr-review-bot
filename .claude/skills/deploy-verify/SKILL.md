---
name: deploy-verify
description: Build this project's deploy image and run a boot-time smoke test before pushing/deploying -- catches a dependency/packaging gap that a green pytest/ruff run cannot, since those run against the full local dev venv, not the image's own scoped/production-only dependency sync.
---

# Deploy verification

`pytest`/`ruff` run against the full workspace dev venv, not the deploy
image's own dependency sync (`--no-dev`, or a `--package <name>`-only sync
in a uv workspace) -- so a dependency declared only in a sub-package's own
`pyproject.toml` but needed at root import time, or a dependency only
present as a dev dependency locally, can pass both checks and still crash
on deploy. This is exactly how the 2026-09-03 `python-multipart` deploy
crash slipped through. **A green test suite does not substitute for this.**

## When to use

Before any push to `main` -- always, regardless of whether the commit
reaching `main` arrived via a merge or was made directly, and not just
when a dependency change "looks" relevant enough to matter.

## How

Run the helper script shipped alongside this skill:

```
bash .claude/skills/deploy-verify/verify_deploy_image.sh [dockerfile] [boot-command]
```

Defaults: `Dockerfile` in the repo root, boot command `uv run --no-sync
python -c "import main"`. Exits non-zero and prints which stage failed
(build vs. boot) if either fails; always cleans up its own tagged image
afterward regardless of outcome.

## Why an import smoke test, not actually starting the server

The real `CMD` starts `uvicorn`, which for this project also runs the
app's full startup lifespan (DB connection, credential loading, etc.) --
that needs real secrets/DB connectivity this skill has no business
touching or requiring. `python -c "import main"` catches the class of bug
this skill exists for (a missing/broken dependency at import time) without
any of that, at the cost of not catching a runtime-only startup failure.
If a project's own lifespan-time bugs become a recurring problem, that's a
reason to extend the boot command (e.g. wrap it with real local test
credentials), not a reason to skip this check.
