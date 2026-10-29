"""Exercise a real worker-process crash between a file edit and its tool receipt."""

import json
import subprocess
from pathlib import Path

from ceo_arena.harness.bun import resolve_bun_executable

ROOT = Path(__file__).resolve().parents[2]


def test_crash_restores_pending_siblings_without_repeating_file_edits_or_paid_reply(
    tmp_path: Path,
) -> None:
    command = [
        resolve_bun_executable(ROOT),
        "run",
        "agent_worker/tests/memory_crash_probe.ts",
        str(tmp_path),
    ]
    crashed = subprocess.run(
        [*command, "crash"], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert crashed.returncode == 86, crashed.stderr
    assert next(tmp_path.rglob("counter.txt")).read_text() == "xx"
    snapshot = json.loads(next(tmp_path.rglob("conversation.json")).read_bytes())
    assert len(snapshot["pending"]["results"]) == 1  # edit happened, but its receipt was not saved

    resumed = subprocess.run(
        [*command, "resume"], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["requests"] == 1  # only a new seal response; no repeat of the first model reply
    assert result["file"].strip() == "1\txx"
    assert result["result"]["status"] == "sealed"
    assert result["result"]["usage"]["provider_turns"] == 2
    assert result["result"]["usage"]["tool_calls"] == 4
    assert result["result"]["diagnostics"]["tool_errors"] == 0
    assert result["result"]["decision"]["products"][0]["price_cents"] == 1500

    cached = subprocess.run(
        [*command, "resume"], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert cached.returncode == 0, cached.stderr
    assert json.loads(cached.stdout)["requests"] == 0
