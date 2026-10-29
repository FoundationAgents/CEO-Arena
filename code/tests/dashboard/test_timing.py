from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ceo_arena.cli import main
from ceo_arena.dashboard import RunDashboard
from ceo_arena.dashboard.timing import SubmissionTimes
from ceo_arena.runtime.performance import PerformanceEvent
from ceo_arena.runtime.trace import TraceKind, TraceRecord

BASE_TIME = datetime(2026, 9, 10, 16, 4, 45, tzinfo=UTC)


def submission(company: str = "company-1", seconds: int = 0) -> bytes:
    return (
        PerformanceEvent(
            recorded_at=BASE_TIME + timedelta(seconds=seconds),
            stage="decision_stage",
            company_id=company,
            window_index=0,
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )


def test_submission_index_handles_live_tail_duplicates_and_replacement(tmp_path: Path) -> None:
    path = tmp_path / "performance.jsonl"
    first, second = submission(), submission("company-2", 60)
    path.write_bytes(first + second[:40])
    index = SubmissionTimes()
    index.refresh(path)
    assert index.get(0, "company-1") == BASE_TIME
    assert index.get(0, "company-2") is None
    with path.open("ab") as stream:
        stream.write(second[40:] + b"invalid optional diagnostic\n" + submission(seconds=120))
    index.refresh(path)
    assert index.get(0, "company-1") == BASE_TIME  # Resume cannot move the first receipt time.
    assert index.get(0, "company-2") == BASE_TIME + timedelta(seconds=60)
    index.refresh(path)
    assert index.get(0, "company-2") == BASE_TIME + timedelta(seconds=60)
    path.write_bytes(submission("company-3"))
    index.refresh(path)
    assert index.get(0, "company-1") is None
    assert index.get(0, "company-3") == BASE_TIME


def test_submission_index_ignores_failed_naive_and_unscoped_events(tmp_path: Path) -> None:
    path = tmp_path / "performance.jsonl"
    events = [
        PerformanceEvent(stage="decision_stage_failed", window_index=0, company_id="company-1"),
        PerformanceEvent(stage="decision_stage", company_id="company-1"),
        PerformanceEvent(
            stage="decision_stage",
            window_index=0,
            company_id="company-1",
            recorded_at=datetime(2026, 9, 10),
        ),
    ]
    path.write_text("\n".join(item.model_dump_json() for item in events) + "\n", encoding="utf-8")
    index = SubmissionTimes()
    index.refresh(path)
    assert index.get(0, "company-1") is None
    path.write_bytes(b"x" * 65_537)
    with pytest.raises(ValueError, match="read limit"):
        index.refresh(path)


def test_dashboard_projects_times_without_changing_artifacts_and_refreshes_cached_receipts(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    assert main(["run-rule", "--companies", "2", "--days", "1", "--output", str(run)]) == 0
    performance = run / "performance.jsonl"
    performance.write_bytes(b"")
    trace = tuple(
        TraceRecord.model_validate_json(raw)
        for raw in (run / "trace.jsonl").read_bytes().splitlines()
    )
    settlement = next(item for item in trace if item.kind is TraceKind.WINDOW_COMMITTED)
    dashboard = RunDashboard(tmp_path)
    run_ref = dashboard.list_runs().runs[0].run_ref
    first = dashboard.get_run(run_ref)
    assert all(item.trajectory[0].submitted_at is None for item in first.companies)
    assert all(item.trajectory[0].settled_at == settlement.recorded_at for item in first.companies)
    assert first.recent_events[-1].recorded_at == trace[-1].recorded_at
    revision = dashboard.revision()
    cached = dashboard._cache[run]
    performance.write_bytes(submission() + submission("company-2", 120))
    artifacts = {path: path.read_bytes() for path in run.iterdir() if path.is_file()}
    detail = dashboard.get_run(run_ref)
    assert dashboard._cache[run] is cached  # Do not reload the world for diagnostic-only changes.
    assert dashboard.revision() != revision
    assert detail.companies[0].trajectory[0].submitted_at == BASE_TIME
    assert detail.companies[1].trajectory[0].submitted_at == BASE_TIME + timedelta(seconds=120)
    assert detail.companies[0].trajectory[0].submitted_at != settlement.recorded_at
    assert all(path.read_bytes() == content for path, content in artifacts.items())
    performance.unlink()
    assert dashboard.get_run(run_ref).companies[0].trajectory[0].submitted_at is None


def test_browser_formats_beijing_time_and_separates_window_timing(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the browser contract test")
    source = Path(__file__).resolve().parents[2] / "src/ceo_arena/dashboard/static/app.js"
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function formatDate(');
const end = source.indexOf('class DashboardApi', start);
const context = {formatInteger: value => String(value)};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const entry = {submitted_at:'2026-09-10T16:04:45Z', settled_at:null,
  stage:'collecting', status:'sealed', decision_duration_ms:65000};
const output = vm.runInContext(`({
  event: eventView({day:21, recorded_at:'2026-09-10T16:04:45Z'}),
  missing: formatDate(null), invalid: formatDate('not-a-date'),
  short: formatDuration(100), missingDuration: formatDuration(null),
  long: formatDuration(3723000),
  collecting: decisionTiming(${JSON.stringify(entry)}),
  failed: decisionTiming({...${JSON.stringify(entry)},
    status:'budget_exhausted', stage:'completed'}),
  pending: decisionTiming({...${JSON.stringify(entry)}, stage:'pending'}),
})`, context);
process.stdout.write(JSON.stringify(output));
"""
    result = subprocess.run(
        [node, "-e", script, str(source)],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    rendered = json.loads(result.stdout)
    assert rendered["event"]["day"] == "Day 21"
    assert rendered["event"]["time"] == "2026-09-11 00:04:45"
    assert (
        rendered["missing"] == rendered["invalid"] == rendered["missingDuration"] == "Not recorded"
    )
    assert rendered["short"] == "<1 s"
    assert rendered["long"] == "1 h 2 m 3 s"
    assert rendered["collecting"] == [
        "Submitted 2026-09-11 00:04:45",
        "Decision time 1 m 5 s",
        "Waiting for other CEOs before settlement",
    ]
    assert rendered["failed"][0].startswith("Decision ended")
    assert rendered["failed"][2] == "Settlement Not recorded"
    assert rendered["pending"][2] == "Awaiting shared settlement"
