"""Pinned Bun runtime discovery shared by setup and execution."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BUN_VERSION = "1.3.14"


def project_bun_path(root: Path) -> Path:
    """Return the project-local direct Bun executable path."""

    directory = root / "agent_worker" / ".tools" / "node_modules" / "bun" / "bin"
    names = ("bun.exe", "bun") if sys.platform == "win32" else ("bun", "bun.exe")
    candidates = tuple(directory / name for name in names)
    return next((path for path in candidates if path.is_file()), candidates[0])


def resolve_bun_executable(root: Path) -> str:
    """Return a direct Bun executable only when its version is pinned."""

    local_bun = project_bun_path(root)
    candidates = (
        os.environ.get("CEO_ARENA_BUN"),
        shutil.which("bun"),
        str(local_bun) if local_bun.is_file() else None,
    )
    for bun in candidates:
        if not bun:
            continue
        try:
            version = subprocess.run(
                (bun, "--version"),
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if version == BUN_VERSION:
            return bun
    raise RuntimeError("Bun 1.3.14 is missing; run the documented worker setup command")
