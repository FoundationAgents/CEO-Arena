"""Prompt-free timing and retry sidecars, separate from economic state and replay."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from ceo_arena.models import ProviderFailure, StrictModel
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime._redact import redact_sensitive_text
from ceo_arena.runtime.persistence import RunCheckpoint, RunManifest


class PerformanceEvent(StrictModel):
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stage: str
    duration_ms: float = Field(default=0, ge=0)
    company_id: str | None = None
    window_index: int | None = None
    attempt: int | None = None
    failure: ProviderFailure | None = None


class CompanyPerformance(StrictModel):
    state: Literal["running", "retrying", "received", "failed", "timed_out"] = "running"
    attempt: int = 1
    last_stage: str = ""
    duration_ms: float = 0
    failure: ProviderFailure | None = None


class PerformanceStatus(StrictModel):
    run_id: str | None = None
    config_hash: str | None = None
    day: int = 0
    total_days: int = 0
    phase: str = "ready"
    active_company_count: int = 0
    window_index: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stage_count: dict[str, int] = Field(default_factory=dict)
    stage_total_ms: dict[str, float] = Field(default_factory=dict)
    companies: dict[str, CompanyPerformance] = Field(default_factory=dict)


class PerformanceRecorder:
    """Record durations without SQL text, tokens, messages, keys or raw response frames."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "performance.jsonl"
        self._status_file = AtomicModelFile(
            directory / "performance_status.json", PerformanceStatus
        )
        self.last_error: str | None = None
        try:
            self._status = self._status_file.load() or PerformanceStatus()
        except (OSError, ValueError) as error:
            self._status = PerformanceStatus()
            self.last_error = type(error).__name__
        self._lock = threading.Lock()

    def bind(self, manifest: RunManifest) -> None:
        with self._lock:
            self._status.run_id = manifest.run_id
            self._status.config_hash = manifest.config_hash
            self._status.total_days = manifest.config.scenario.total_days

    def update_frontier(self, checkpoint: RunCheckpoint) -> None:
        """Publish only a successfully saved economic frontier; this remains diagnostic."""
        with self._lock:
            self._status.day = checkpoint.current_state.day
            self._status.phase = checkpoint.phase.value
            self._status.window_index = checkpoint.next_window_index
            self._status.active_company_count = sum(
                company.status.value == "active"
                for company in checkpoint.current_state.companies.values()
            )

    @contextmanager
    def measure(
        self, stage: str, *, company_id: str | None = None, window_index: int | None = None
    ) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                stage,
                (time.perf_counter() - started) * 1000,
                company_id=company_id,
                window_index=window_index,
            )

    def record(
        self,
        stage: str,
        duration_ms: float = 0,
        *,
        company_id: str | None = None,
        window_index: int | None = None,
        attempt: int | None = None,
        failure: ProviderFailure | None = None,
    ) -> None:
        if failure is not None:
            failure = failure.model_copy(
                update={
                    "message": (redact_sensitive_text(failure.message) or "")[:2000],
                    "reason": (redact_sensitive_text(failure.reason) or "")[:256] or None,
                }
            )
        event = PerformanceEvent(
            stage=stage,
            duration_ms=max(0, duration_ms),
            company_id=company_id,
            window_index=window_index,
            attempt=attempt,
            failure=failure,
        )
        with self._lock:
            try:
                self._append(event)
            except OSError as error:
                # Losing optional measurements must not cancel healthy CEOs or hide an API error.
                self.last_error = type(error).__name__

    def _append(self, event: PerformanceEvent) -> None:
        stage, company_id = event.stage, event.company_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(event.model_dump_json() + "\n")
        self._status.updated_at = event.recorded_at
        counts, totals = self._status.stage_count, self._status.stage_total_ms
        counts[stage] = counts.get(stage, 0) + 1
        totals[stage] = totals.get(stage, 0) + event.duration_ms
        if company_id is not None:
            company = self._status.companies.setdefault(company_id, CompanyPerformance())
            company.last_stage, company.duration_ms = stage, event.duration_ms
            if event.attempt is not None:
                company.attempt = event.attempt
            if stage == "provider_retry":
                company.state, company.failure = "retrying", event.failure
            elif stage == "decision_stage":
                company.state, company.failure = "received", None
            elif stage == "provider_timeout":
                company.state, company.failure = "timed_out", event.failure
            elif stage in {
                "decision_stage_failed",
                "provider_permanent_failure",
            }:
                company.state, company.failure = "failed", event.failure
            elif stage == "decision_attempt":
                company.state = "running"
        # Query/validation events stay append-only; only lifecycle boundaries publish status.
        if stage in {
            "provider_retry",
            "provider_permanent_failure",
            "provider_timeout",
            "decision_attempt",
            "decision_stage",
            "decision_stage_failed",
            "checkpoint",
            "settlement",
        }:
            self._status_file.save(self._status)
