"""Stable fingerprints for code that can change decisions or economic outcomes."""

from __future__ import annotations

import hashlib
import platform
from importlib.metadata import version
from pathlib import Path

from ceo_arena.harness import OPENCODE_LLM_COMMIT
from ceo_arena.harness.bun import BUN_VERSION
from ceo_arena.protocol import DECISION_PROTOCOL_VERSION


def simulation_fingerprints(root: Path | None = None) -> dict[str, str]:
    """Hash simulation, Harness, and capacity-policy code while excluding presentation."""

    project_root = (root or Path(__file__).resolve().parents[2]).resolve()
    package = project_root / "src" / "ceo_arena"
    files = [
        project_root / ".python-version",
        project_root / "agent_worker" / "package.json",
        project_root / "agent_worker" / "tsconfig.json",
        project_root / "agent_worker" / "tsconfig.typecheck.json",
        project_root / "OpenCode" / "bun.lock",
        package / "config.py",
        package / "execution.py",
        package / "hashing.py",
        package / "inference.py",
        package / "models.py",
        package / "protocol.py",
        package / "harness" / "rule_profile.json",
        package / "campaign" / "resources.py",
        package / "campaign" / "models.py",
        package / "campaign" / "records.py",
        package / "campaign" / "scheduler.py",
        *(project_root / "agent_worker" / "src").rglob("*.ts"),
        *(package / "economy").rglob("*.py"),
        *(package / "evaluation").rglob("*.py"),
        *(package / "harness").rglob("*.py"),
        *(package / "runtime").rglob("*.py"),
    ]
    return {
        "decision_protocol_version": DECISION_PROTOCOL_VERSION,
        "opencode_commit": OPENCODE_LLM_COMMIT,
        "bun_version": BUN_VERSION,
        "python_version": platform.python_version(),
        "dependency:httpx": version("httpx"),
        "dependency:numpy": version("numpy"),
        "dependency:pydantic": version("pydantic"),
        **{
            path.relative_to(project_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(files))
            if path.is_file()
        },
    }


__all__ = ["simulation_fingerprints"]
