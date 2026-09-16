from pathlib import Path

ROOT = Path(__file__).parent.parent
DEMO = (ROOT / "Dockerfile.demo").read_text()
REAL = (ROOT / "Dockerfile").read_text()


def _live_lines(text: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_demo_image_runs_the_demo_entrypoint():
    assert "demo.app:app" in DEMO
    assert "main:app" not in DEMO


def test_demo_image_ships_its_own_fake_settings():
    for key in ("GITHUB_WEBHOOK_SECRET", "DASHBOARD_USERNAME",
                "DASHBOARD_PASSWORD", "DASHBOARD_SESSION_SECRET"):
        assert key in DEMO, f"{key} must ship in the image, not be set on Render"


def test_demo_image_never_declares_a_database_url():
    assert "DATABASE_URL" not in DEMO


def test_real_image_never_contains_the_demo_package():
    assert "demo" not in _live_lines(REAL)[-1]
    assert "COPY demo" not in REAL


def test_no_recursive_chown():
    # Mirrors tests/test_dockerfile.py: an overlay fs copies every chowned
    # file into a new layer.
    assert not any(line.startswith("RUN chown -R") or "chown -R" in line
                   for line in _live_lines(DEMO))
