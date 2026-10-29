"""Observation-only system resource and decision-health telemetry."""

from __future__ import annotations

import ctypes
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ceo_arena.models import StrictModel

DECISION_HEALTH_WINDOW_SIZE = 100


class ResourceSample(StrictModel):
    """One nonblocking measurement; missing measurements remain explicitly unknown."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    available_memory_mb: float | None = Field(default=None, ge=0)
    total_memory_mb: float | None = Field(default=None, ge=0)
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    commit_percent: float | None = Field(default=None, ge=0)
    available_commit_mb: float | None = Field(default=None, ge=0)
    event_loop_lag_seconds: float = Field(default=0.0, ge=0)
    probe_error: str | None = None


class DecisionHealth(StrictModel):
    """Rolling completed decisions, excluding queue wait and protocol errors from failures."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    completed_count: int = Field(default=0, ge=0)
    total_completed_count: int = Field(default=0, ge=0)
    timeout_count: int = Field(default=0, ge=0)
    provider_error_count: int = Field(default=0, ge=0)
    median_latency_seconds: float | None = Field(default=None, ge=0)
    baseline_latency_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_counts(self) -> DecisionHealth:
        if self.timeout_count + self.provider_error_count > self.completed_count:
            raise ValueError("decision failures cannot exceed completed decisions")
        return self

    @property
    def error_ratio(self) -> float:
        return (self.timeout_count + self.provider_error_count) / max(1, self.completed_count)

    @property
    def latency_ratio(self) -> float | None:
        if self.median_latency_seconds is None or self.baseline_latency_seconds is None:
            return None
        return self.median_latency_seconds / self.baseline_latency_seconds


ResourceStatus = Literal[
    "healthy", "holding", "pressure", "critical", "unavailable", "disabled", "monitoring"
]


class SchedulerSnapshot(StrictModel):
    """Bounded dashboard state describing admission targets without cancelling live work."""

    timestamp: datetime
    scheduler_running: bool = True
    run_limit: int = Field(ge=1)
    decision_limit: int | None = Field(default=None, ge=1)
    max_runs: int = Field(ge=1)
    max_decisions: int | None = Field(default=None, ge=1)
    active_runs: int = Field(ge=0)
    active_decisions: int = Field(ge=0)
    resident_workers: int = Field(default=0, ge=0)
    waiting_decisions: int = Field(default=0, ge=0)
    sample_interval_seconds: float = Field(gt=0)
    allow_new_runs: bool
    resource_status: ResourceStatus
    reasons: tuple[str, ...]
    adjustment: Literal["grow", "reduce", "hold", "disabled", "monitoring"]
    resource: ResourceSample
    health: DecisionHealth


class _WindowsPerformanceInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        *[
            (name, ctypes.c_size_t)
            for name in (
                "commit_total",
                "commit_limit",
                "commit_peak",
                "physical_total",
                "physical_available",
                "system_cache",
                "kernel_total",
                "kernel_paged",
                "kernel_nonpaged",
                "page_size",
            )
        ],
        ("handle_count", ctypes.c_uint32),
        ("process_count", ctypes.c_uint32),
        ("thread_count", ctypes.c_uint32),
    ]


class SystemResourceProbe:
    """Read Windows or Linux system counters without sleeping or adding dependencies."""

    def __init__(self) -> None:
        self._previous_cpu: tuple[int, int] | None = None

    def sample(self, *, event_loop_lag_seconds: float = 0.0) -> ResourceSample:
        try:
            if sys.platform == "win32":
                sample = self._windows()
            elif sys.platform.startswith("linux"):
                sample = self._linux()
            else:
                raise OSError("unsupported resource probe platform")
            return sample.model_copy(
                update={"event_loop_lag_seconds": max(0.0, event_loop_lag_seconds)}
            )
        except (AttributeError, IndexError, OSError, TypeError, ValueError) as error:
            self._previous_cpu = None
            return ResourceSample(
                event_loop_lag_seconds=max(0.0, event_loop_lag_seconds),
                probe_error=f"{sys.platform}: {type(error).__name__}",
            )

    def _cpu_percent(self, total: int, idle: int) -> float | None:
        previous, self._previous_cpu = self._previous_cpu, (total, idle)
        if previous is None or total <= previous[0] or idle < previous[1]:
            return None
        return min(100.0, max(0.0, 100.0 * (1 - (idle - previous[1]) / (total - previous[0]))))

    def _windows(self) -> ResourceSample:
        performance = ctypes.WinDLL("psapi", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        info = _WindowsPerformanceInfo(cb=ctypes.sizeof(_WindowsPerformanceInfo))
        performance.GetPerformanceInfo.argtypes = [
            ctypes.POINTER(_WindowsPerformanceInfo),
            ctypes.c_uint32,
        ]
        performance.GetPerformanceInfo.restype = ctypes.c_int
        if not performance.GetPerformanceInfo(ctypes.byref(info), info.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        idle, kernel_time, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        kernel.GetSystemTimes.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 3
        kernel.GetSystemTimes.restype = ctypes.c_int
        if not kernel.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel_time), ctypes.byref(user)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        page_mb = info.page_size / (1024 * 1024)
        return ResourceSample(
            total_memory_mb=info.physical_total * page_mb,
            available_memory_mb=info.physical_available * page_mb,
            commit_percent=100.0 * info.commit_total / info.commit_limit
            if info.commit_limit
            else None,
            available_commit_mb=max(0, info.commit_limit - info.commit_total) * page_mb,
            cpu_percent=self._cpu_percent(kernel_time.value + user.value, idle.value),
        )

    def _linux(self) -> ResourceSample:
        memory = {
            name: int(value.split()[0])
            for name, value in (
                line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
            )
        }
        cpu = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
        if len(cpu) < 4:
            raise ValueError("invalid CPU counters")
        # Linux commit accounting is an admission limit only under strict overcommit mode.
        strict_commit = Path("/proc/sys/vm/overcommit_memory").read_text().strip() == "2"
        commit_limit = memory.get("CommitLimit", 0) if strict_commit else 0
        committed = memory.get("Committed_AS", 0)
        return ResourceSample(
            total_memory_mb=memory["MemTotal"] / 1024,
            available_memory_mb=memory["MemAvailable"] / 1024,
            cpu_percent=self._cpu_percent(sum(cpu), cpu[3] + (cpu[4] if len(cpu) > 4 else 0)),
            commit_percent=100.0 * committed / commit_limit if commit_limit else None,
            available_commit_mb=max(0, commit_limit - committed) / 1024 if commit_limit else None,
        )
