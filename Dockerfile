FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --no-sync is required because dashboard (a workspace member) never gets a
# real `uv sync --package dashboard` in this image; a plain `uv run` would
# try to re-sync the whole workspace and fail for the same missing-lockfile
# reason as above.
CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
