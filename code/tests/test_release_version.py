"""The package and dashboard must report the version declared for installation."""

from pathlib import Path
from tomllib import loads

from ceo_arena import __version__


def test_public_version_matches_project_metadata() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert __version__ == loads(project.read_text(encoding="utf-8"))["project"]["version"]
