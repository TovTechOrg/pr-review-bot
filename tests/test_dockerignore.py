"""Guards on the Docker build context's exclusion list.

A build context is not the image: `Dockerfile` uses an explicit COPY list, so
an un-ignored key never reached a layer. It does reach the daemon, though,
and it would land in an image the moment anyone adds a broad COPY -- which is
exactly the kind of latent gap worth pinning rather than re-deriving.
"""

from pathlib import Path

DOCKERIGNORE = (Path(__file__).parent.parent / ".dockerignore").read_text()


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_pem_files_are_excluded():
    assert "*.pem" in _patterns()


def test_every_private_key_json_is_excluded():
    """Regression: the only JSON pattern was `gcp-service-account-key*.json`,
    which does not match the real on-disk `vertex-ai-private-key.json`."""
    patterns = _patterns()
    assert "*private-key*.json" in patterns
    assert "*service-account*.json" in patterns


def test_the_real_vertex_key_filename_would_match_a_pattern():
    """Named after the actual file this gap was found against."""
    import fnmatch

    filename = "vertex-ai-private-key.json"
    assert any(fnmatch.fnmatch(filename, pattern) for pattern in _patterns())


def test_env_files_are_excluded():
    patterns = _patterns()
    assert ".env" in patterns
    assert "**/.env" in patterns
