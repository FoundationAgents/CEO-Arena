"""Offline tests for live run navigation and bounded metadata discovery."""

import shutil
import subprocess
from pathlib import Path

import pytest

from ceo_arena.dashboard.discovery import metadata_files


def test_navigation_browser_contracts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser contract tests")
    subprocess.run(
        [node, "--test", str(Path(__file__).with_name("browser.test.cjs"))],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )


def test_discovery_skips_private_histories_and_recovery_backups(tmp_path: Path) -> None:
    visible = tmp_path / "campaign" / "runs" / "seed-11" / "lineup-1"
    visible.mkdir(parents=True)
    (visible / "manifest.json").write_text("{}")
    for name in ("agent-memory", "agent-traces", "allowances", "recovery-patches"):
        hidden = visible / name / "nested"
        hidden.mkdir(parents=True)
        (hidden / "manifest.json").write_text("{}")
    assert list(metadata_files(tmp_path, "manifest.json")) == [visible / "manifest.json"]
