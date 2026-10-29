"""Contracts for observation-only telemetry; no adaptive admission controller."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ceo_arena.campaign.resources import (
    DecisionHealth,
    ResourceSample,
    SchedulerSnapshot,
    SystemResourceProbe,
)


def test_monitoring_snapshot_round_trips_without_model_limits() -> None:
    now = datetime(2026, 9, 11, tzinfo=UTC)
    snapshot = SchedulerSnapshot(
        timestamp=now,
        run_limit=20,
        max_runs=20,
        active_runs=3,
        active_decisions=24,
        sample_interval_seconds=10,
        allow_new_runs=True,
        resource_status="monitoring",
        reasons=(),
        adjustment="monitoring",
        resource=ResourceSample(timestamp=now),
        health=DecisionHealth(),
    )
    restored = SchedulerSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot
    assert restored.decision_limit is None and restored.max_decisions is None
    assert restored.timestamp.utcoffset().total_seconds() == 0


def test_health_does_not_invent_measurements_and_rejects_invalid_counts() -> None:
    assert DecisionHealth().latency_ratio is None
    assert DecisionHealth().error_ratio == 0
    with pytest.raises(ValueError, match="cannot exceed"):
        DecisionHealth(completed_count=1, provider_error_count=2)


def test_cpu_delta_is_nonblocking_and_handles_reset_counters() -> None:
    probe = SystemResourceProbe()
    assert probe._cpu_percent(100, 40) is None
    assert probe._cpu_percent(200, 60) == 80
    assert probe._cpu_percent(200, 60) is None
    assert probe._cpu_percent(10, 2) is None


@pytest.mark.parametrize("overcommit, expected_commit", [("0", None), ("1", None), ("2", 95.0)])
def test_linux_probe_uses_available_memory_and_only_enforced_commit_limits(
    monkeypatch: pytest.MonkeyPatch,
    overcommit: str,
    expected_commit: float | None,
) -> None:
    files = {
        "/proc/meminfo": "MemTotal: 16777216 kB\nMemAvailable: 2097152 kB\n"
        "CommitLimit: 100000 kB\nCommitted_AS: 95000 kB\n",
        "/proc/stat": "cpu  100 0 100 800 100 0 0 0 99 99\n",
        "/proc/sys/vm/overcommit_memory": overcommit,
    }
    monkeypatch.setattr(Path, "read_text", lambda path: files[path.as_posix()])
    probe = SystemResourceProbe()
    sample = probe._linux()
    assert (sample.total_memory_mb, sample.available_memory_mb) == (16384, 2048)
    assert sample.commit_percent == expected_commit
    assert sample.cpu_percent is None
    files["/proc/stat"] = "cpu  150 0 150 850 150 0 0 0 199 199\n"
    assert probe._linux().cpu_percent == 50


def test_probe_errors_are_explicit_without_exposing_os_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> ResourceSample:
        raise OSError("private system detail")

    monkeypatch.setattr(SystemResourceProbe, "_windows", lambda self: fail())
    monkeypatch.setattr(SystemResourceProbe, "_linux", lambda self: fail())
    sample = SystemResourceProbe().sample(event_loop_lag_seconds=0.75)
    assert sample.probe_error is not None
    assert "private system detail" not in sample.probe_error
    assert sample.available_memory_mb is None
    assert sample.event_loop_lag_seconds == 0.75
