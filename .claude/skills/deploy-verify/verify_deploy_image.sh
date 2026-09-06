#!/usr/bin/env bash
# Builds this project's deploy image and runs a boot-time smoke test --
# catches a dependency/packaging gap that a green `pytest`/`ruff` run
# cannot, since those run against the full local dev venv, not the image's
# own scoped/production-only dependency sync (e.g. `--no-dev`, or a
# `--package <name>`-only sync in a uv workspace). See the sibling
# SKILL.md for the incident this generalizes from.
#
# Usage: verify_deploy_image.sh [dockerfile] [boot-command]
#   dockerfile    path to the Dockerfile to build (default: Dockerfile)
#   boot-command  command run inside the built image as the boot check
#                 (default: `uv run --no-sync python -c "import main"` --
#                 an import-time smoke test that catches a missing/broken
#                 dependency without needing real secrets or DB
#                 connectivity that the app's actual runtime lifespan
#                 might require. `uv run --no-sync` matches how this
#                 image's own CMD invokes uv -- a bare `python` is not on
#                 PATH inside a uv-managed venv, so plain `python -c ...`
#                 fails with a spurious ModuleNotFoundError, not a real one)
set -euo pipefail

DOCKERFILE="${1:-Dockerfile}"
BOOT_CMD="${2:-uv run --no-sync python -c \"import main\"}"
TAG="deploy-verify-$$"

echo "==> Building ${DOCKERFILE} as ${TAG} ..."
docker build -f "$DOCKERFILE" -t "$TAG" .

echo "==> Boot check: ${BOOT_CMD}"
status=0
docker run --rm "$TAG" sh -c "$BOOT_CMD" || status=$?

docker rmi "$TAG" >/dev/null 2>&1 || true

if [ "$status" -eq 0 ]; then
    echo "==> PASS: image builds and boots."
else
    echo "==> FAIL: image built but the boot check failed (exit ${status}) -- see output above."
fi
exit "$status"
