"""Regression coverage for Dockerfile conventions that aren't obvious from
reading the file alone -- see CLAUDE.md's "Docker image: no chown -R"
section for the full rationale."""

from __future__ import annotations

from pathlib import Path

DOCKERFILE = (Path(__file__).parent.parent / "Dockerfile").read_text()


def test_dockerfile_never_chowns_app_directory():
    """`chown -R appuser:appuser /app` was measured to add +110MB/+29% image
    size for zero functional benefit -- overlayfs stores a changed file as
    a full copy, not a diff, so chowning everything just-copied duplicates
    the whole venv + app code into a new layer. Nothing under /app is
    written to at runtime (the only filesystem access is a read-only
    StaticFiles mount), so appuser only ever needs the read+execute
    permissions COPY/RUN already leave in place by default. If a future
    change genuinely needs appuser to own or write to something under
    /app, chown only that specific path (or use `COPY --chown=`), not a
    blanket `-R /app`."""
    for line in DOCKERFILE.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # explaining the tradeoff in prose is fine -- no actual chown command
        assert "chown" not in stripped, f"found a live chown command: {stripped!r}"


def test_dockerfile_still_drops_root_before_cmd():
    """The no-chown fix must not silently regress into running the
    container as root -- appuser still needs to exist and be switched to
    before CMD."""
    assert "useradd -m -u 1000 appuser" in DOCKERFILE
    assert "USER appuser" in DOCKERFILE
    assert DOCKERFILE.index("useradd -m -u 1000 appuser") < DOCKERFILE.index("USER appuser")
    assert DOCKERFILE.index("USER appuser") < DOCKERFILE.index("CMD [")
