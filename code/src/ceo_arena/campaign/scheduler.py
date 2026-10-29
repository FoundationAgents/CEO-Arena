"""Fixed-cap Run backfill with observation-only system telemetry."""

from __future__ import annotations

import asyncio
import os
import statistics
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from ceo_arena.campaign.models import FrozenCampaignPlan
from ceo_arena.campaign.records import (
    FailurePhase,
    RunJobRecord,
    RunJobStatus,
)
from ceo_arena.campaign.repository import (
    CampaignRepository,
    LeaseLostError,
    RevisionConflictError,
)
from ceo_arena.campaign.resources import (
    DECISION_HEALTH_WINDOW_SIZE,
    DecisionHealth,
    ResourceSample,
    SchedulerSnapshot,
    SystemResourceProbe,
)
from ceo_arena.harness.worker_pool import WorkerPool
from ceo_arena.models import DecisionWindowResult, StrictModel
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.admission import BoundedDecisionAdmission, UnlimitedDecisionAdmission

SCHEDULER_STATUS_FILE = "scheduler_status.json"
SCHEDULER_EVENTS_FILE = "scheduler_events.jsonl"

type ModelAdmission = BoundedDecisionAdmission | UnlimitedDecisionAdmission
type RunExecutor = Callable[[RunJobRecord, str, ModelAdmission], Awaitable[None]]


class ResourceProbe(Protocol):
    def sample(self, *, event_loop_lag_seconds: float = 0.0) -> ResourceSample: ...


class SchedulerEvent(StrictModel):
    """One resource sample tied to an immutable experiment and scheduler session."""

    kind: Literal["started", "sample", "stopped"]
    session_id: str
    plan_hash: str
    snapshot: SchedulerSnapshot


class _DecisionHealthWindow:
    """Keep bounded infrastructure observations without model identity or economic data."""

    def __init__(self, baseline_size: int) -> None:
        self._observations: deque[tuple[str, float]] = deque(maxlen=DECISION_HEALTH_WINDOW_SIZE)
        self._baseline_size = baseline_size
        self._baseline: float | None = None
        self._total = 0

    def record(self, result: DecisionWindowResult, duration: float) -> None:
        self._total += 1
        self._observations.append((result.status, max(0.001, duration)))
        successful = [elapsed for status, elapsed in self._observations if status == "sealed"]
        if self._baseline is None and len(successful) >= self._baseline_size:
            self._baseline = statistics.median(successful[: self._baseline_size])

    def summary(self) -> DecisionHealth:
        durations = [elapsed for _, elapsed in self._observations]
        return DecisionHealth(
            completed_count=len(durations),
            total_completed_count=self._total,
            timeout_count=sum(status == "timeout" for status, _ in self._observations),
            provider_error_count=sum(
                status == "provider_error" for status, _ in self._observations
            ),
            median_latency_seconds=statistics.median(durations) if durations else None,
            baseline_latency_seconds=self._baseline,
        )


class CampaignScheduler:
    """Fill available Run slots in seed order and drain naturally when limits fall."""

    def __init__(
        self,
        plan: FrozenCampaignPlan,
        repository: CampaignRepository,
        output: Path,
        execute: RunExecutor,
        worker_pool: WorkerPool,
        *,
        resource_probe: ResourceProbe | None = None,
        lease_seconds: float = 120.0,
        poll_seconds: float = 1.0,
    ) -> None:
        self._plan = plan
        self._repository = repository
        self._execute = execute
        self._pool = worker_pool
        self._probe = resource_probe or SystemResourceProbe()
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._session_id = uuid.uuid4().hex
        self._owner = f"{os.getpid()}-{self._session_id}"
        limit = plan.execution.max_inflight_decisions
        self.admission: ModelAdmission = (
            UnlimitedDecisionAdmission() if limit is None else BoundedDecisionAdmission(limit)
        )
        self._health = _DecisionHealthWindow(5)
        self._active: dict[str, asyncio.Task[None]] = {}
        self._snapshot: SchedulerSnapshot | None = None
        self._status = AtomicModelFile(output / SCHEDULER_STATUS_FILE, SchedulerSnapshot)
        self._events_path = output / SCHEDULER_EVENTS_FILE
        self._plan_hash = plan.plan_hash
        self.last_telemetry_error: str | None = None

    def observe_decision(self, result: DecisionWindowResult, duration: float) -> None:
        """Record only completed, actively timed model decisions for load control."""

        self._health.record(result, duration)

    async def run(self) -> None:
        """Run every eligible task without crossing a live lease or losing retry work."""

        try:
            await self._sample(kind="started")
            next_sample = time.monotonic() + self._plan.execution.resource_sample_interval_seconds
            while True:
                now = time.monotonic()
                if now >= next_sample:
                    await self._sample(event_loop_lag_seconds=max(0.0, now - next_sample))
                    next_sample = (
                        time.monotonic() + self._plan.execution.resource_sample_interval_seconds
                    )
                self._fill_available_slots()
                if not self._active and self._all_finished():
                    return
                timeout = min(self._poll_seconds, max(0.001, next_sample - time.monotonic()))
                if not self._active:
                    await asyncio.sleep(timeout)
                    continue
                completed, _ = await asyncio.wait(
                    self._active.values(),
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for key, task in tuple(self._active.items()):
                    if task in completed:
                        del self._active[key]
                        self._settle_task(key, task)
                if completed:
                    self._publish_status()
        finally:
            tasks = tuple(self._active.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._active.clear()
            if self._snapshot is not None:
                self._snapshot = self._snapshot.model_copy(update={"scheduler_running": False})
                self._publish_status(kind="stopped")

    async def _sample(
        self,
        *,
        kind: Literal["started", "sample"] = "sample",
        event_loop_lag_seconds: float = 0.0,
    ) -> None:
        try:
            sample = self._probe.sample(event_loop_lag_seconds=event_loop_lag_seconds)
        except Exception as error:
            sample = ResourceSample(
                event_loop_lag_seconds=event_loop_lag_seconds,
                probe_error=type(error).__name__,
            )
        self._snapshot = SchedulerSnapshot(
            timestamp=sample.timestamp,
            run_limit=self._plan.execution.max_concurrent_runs,
            decision_limit=self.admission.limit,
            max_runs=self._plan.execution.max_concurrent_runs,
            max_decisions=self._plan.execution.max_inflight_decisions,
            active_runs=len(self._active),
            active_decisions=self.admission.active_count,
            sample_interval_seconds=self._plan.execution.resource_sample_interval_seconds,
            allow_new_runs=True,
            resource_status="unavailable" if sample.probe_error else "monitoring",
            reasons=("observation_only_no_admission_changes",),
            adjustment="monitoring",
            resource=sample,
            health=self._health.summary(),
        )
        self._publish_status(kind=kind)

    def _settle_task(self, task_key: str, task: asyncio.Task[None]) -> None:
        """Isolate one executor failure; healthy siblings keep their leases and work."""
        try:
            task.result()
        except (Exception, asyncio.CancelledError) as error:
            job = self._repository.get_job(task_key)
            if (
                job is None
                or job.status is not RunJobStatus.RUNNING
                or job.lease_owner != self._owner
            ):
                return
            with suppress(LeaseLostError, RevisionConflictError):
                self._repository.interrupt_run(
                    task_key,
                    lease_owner=self._owner,
                    expected_revision=job.revision,
                    error=f"executor failed: {type(error).__name__}",
                    phase=FailurePhase.RUNTIME,
                    retry_delay_seconds=self._poll_seconds,
                )

    def _fill_available_slots(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.allow_new_runs:
            return
        while len(self._active) + self._external_leases() < snapshot.run_limit:
            job = self._claim_next_ready()
            if job is None:
                return
            self._active[job.task_key] = asyncio.create_task(
                self._execute(job, self._owner, self.admission),
                name=f"campaign:{self._plan.campaign_id}:seed:{job.seed}:{job.run_id}",
            )
            self._publish_status()

    def _claim_next_ready(self) -> RunJobRecord | None:
        seeds = self._plan.seeds
        if self._plan.execution.schedule == "seed_wave":
            runnable = {
                job.seed
                for job in self._repository.list_jobs(self._plan.campaign_id)
                if job.status.is_scheduler_runnable
            }
            seeds = tuple(seed for seed in seeds if seed in runnable)[:1]
        for seed in seeds:
            job = self._repository.claim_next(
                self._plan.campaign_id,
                lease_owner=self._owner,
                lease_seconds=self._lease_seconds,
                seed=seed,
                exclude_task_keys=tuple(self._active),
            )
            if job is not None:
                return job
        return None

    def _external_leases(self) -> int:
        """Reserve slots for still-live leases inherited after a process restart."""

        now = datetime.now(UTC)
        return sum(
            job.status is RunJobStatus.RUNNING
            and job.task_key not in self._active
            and job.lease_until is not None
            and job.lease_until > now
            for job in self._repository.list_jobs(self._plan.campaign_id)
        )

    def _all_finished(self) -> bool:
        return all(
            job.status.is_terminal or job.status is RunJobStatus.STOPPED
            for job in self._repository.list_jobs(self._plan.campaign_id)
        )

    def _publish_status(
        self, *, kind: Literal["started", "sample", "stopped"] | None = None
    ) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        snapshot = snapshot.model_copy(
            update={
                "active_runs": len(self._active),
                "active_decisions": self.admission.active_count,
                "resident_workers": self._pool.resident_count,
                "waiting_decisions": self.admission.waiting_count,
                "health": self._health.summary(),
            }
        )
        try:
            self._write_status(snapshot, kind=kind)
        except OSError as error:
            # Optional monitoring cannot revoke healthy Run leases or alter admission.
            self.last_telemetry_error = type(error).__name__
        else:
            self.last_telemetry_error = None

    def _write_status(
        self,
        snapshot: SchedulerSnapshot,
        *,
        kind: Literal["started", "sample", "stopped"] | None,
    ) -> None:
        """Keep all optional scheduler I/O behind one failure-isolation boundary."""
        self._status.save(snapshot)
        if kind is not None:
            event = SchedulerEvent(
                kind=kind,
                session_id=self._session_id,
                plan_hash=self._plan_hash,
                snapshot=snapshot,
            )
            with self._events_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(event.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())


__all__ = [
    "SCHEDULER_EVENTS_FILE",
    "SCHEDULER_STATUS_FILE",
    "CampaignScheduler",
    "ResourceProbe",
]
