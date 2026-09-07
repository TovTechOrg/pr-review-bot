FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Created early so USER can drop to it below. Deliberately never
# `chown -R`'d to it: nothing under /app is ever written to at runtime
# (verified -- the only filesystem access here is a read-only StaticFiles
# mount), so appuser only ever needs the read+execute permissions COPY/RUN
# already leave in place by default. A chown in its own layer would
# duplicate the entire venv + app code into a new layer (overlayfs stores a
# changed file as a full copy, not a diff) -- measured at +110MB/+29% image
# size for zero functional benefit.
RUN useradd -m -u 1000 appuser

COPY pyproject.toml uv.lock ./
# dashboard's code (and, critically, its pyproject.toml) must be physically
# present for uv run's workspace discovery to succeed at runtime -- even
# though this image never runs `uv sync --package dashboard`. Without this,
# `uv run` fails: "Workspace member /app/dashboard is missing a
# pyproject.toml", and the container crash-loops.
COPY dashboard ./dashboard
RUN uv sync --frozen --no-dev --package pr-review-bot

COPY config.py config_deps.py diff_utils.py formatting.py \
     github_app.py hmac_verify.py main.py orchestrator.py render_client.py \
     webhook.py ./
COPY providers ./providers
COPY review_queue ./review_queue
COPY specialists ./specialists

USER appuser

EXPOSE 8000

# --no-sync is required because dashboard (a workspace member) never gets a
# real `uv sync --package dashboard` in this image; a plain `uv run` would
# try to re-sync the whole workspace and fail for the same missing-lockfile
# reason as above.
CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
