"""Exercise durable scheduling with controlled Runs and no provider requests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.models import CampaignExecutionSpec
from ceo_arena.campaign.records import (
    RunJobRecord,
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.campaign.repository import (
    CampaignRepository,
)
from ceo_arena.campaign.resources import ResourceSample, SchedulerSnapshot
from ceo_arena.campaign.scheduler import (
    SCHEDULER_EVENTS_FILE,
    SCHEDULER_STATUS_FILE,
    CampaignScheduler,
    SchedulerEvent,
)
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.harness.worker_pool import WorkerKey, WorkerPool
from ceo_arena.models import DecisionWindowResult
from ceo_arena.runtime.admission import BoundedDecisionAdmission


class _Probe:
    def __init__(self) -> None:
        self.reading = ResourceSample(
            total_memory_mb=16384,
            available_memory_mb=8192,
            available_commit_mb=8192,
            commit_percent=40,
            cpu_percent=30,
        )
        self.samples = 0

    def sample(self, *, event_loop_lag_seconds: float = 0.0) -> ResourceSample:
        del event_loop_lag_seconds
        self.samples += 1
        return self.reading.model_copy(update={"timestamp": datetime.now(UTC)})


class _Executor:
    def __init__(self, repository: CampaignRepository) -> None:
        self.repository = repository
        self.started: list[RunJobRecord] = []
        self.active: dict[str, asyncio.Event] = {}
        self.cancelled: list[str] = []
        self.peak = 0
        self._arrivals: asyncio.Queue[RunJobRecord] = asyncio.Queue()
        self._draining = False

    async def __call__(
        self, job: RunJobRecord, owner: str, admission: BoundedDecisionAdmission
    ) -> None:
        del admission
        release = asyncio.Event()
        self.active[job.task_key] = release
        self.started.append(job)
        self.peak = max(self.peak, len(self.active))
        self._arrivals.put_nowait(job)
        try:
            if not self._draining:
                await release.wait()
            self.repository.stop_run(
                job.task_key,
                lease_owner=owner,
                expected_revision=job.revision,
                reason=RunStopReason.USER_REQUESTED,
                error="offline dispatch test finished",
            )
        except asyncio.CancelledError:
            self.cancelled.append(job.task_key)
            raise
        finally:
            self.active.pop(job.task_key)

    async def next_started(self) -> RunJobRecord:
        return await asyncio.wait_for(self._arrivals.get(), timeout=5)

    def release(self, job: RunJobRecord) -> None:
        self.active[job.task_key].set()

    def drain(self) -> None:
        self._draining = True
        for release in self.active.values():
            release.set()


class _Worker:
    async def close(self) -> None:
        pass

    async def kill(self) -> None:
        pass


class _Case:
    def __init__(
        self,
        output: Path,
        *,
        seeds: tuple[int, ...] = (47, 11, 29),
        max_runs: int = 2,
        max_decisions: int | None = None,
        model_ids: tuple[str, ...] = ("model-one", "model-two"),
    ) -> None:
        spec = ExperimentSpec(
            campaign_id="scheduler-test",
            base_url="https://gateway.example/v1",
            company_count=len(model_ids),
            days=1,
            seeds=seeds,
            api=(ApiSpec(api_key="offline-only", models=model_ids),),
            execution=CampaignExecutionSpec(
                max_concurrent_runs=max_runs,
                max_inflight_decisions=max_decisions,
                resource_sample_interval_seconds=0.025,
            ),
        )
        self.application = CampaignApplication(spec, CampaignCompiler().compile(spec), output)
        self.application.prepare()
        self.repository = self.application.repository
        self.probe = _Probe()
        self.executor = _Executor(self.repository)
        self.pool: WorkerPool[_Worker] = WorkerPool(capacity=max_decisions)
        self.scheduler = CampaignScheduler(
            self.application.plan,
            self.repository,
            self.application.output,
            self.executor,
            self.pool,
            resource_probe=self.probe,
            poll_seconds=0.005,
        )

    @property
    def jobs(self) -> tuple[RunJobRecord, ...]:
        return self.repository.list_jobs(self.application.plan.campaign_id)

    @property
    def status(self) -> SchedulerSnapshot:
        return SchedulerSnapshot.model_validate_json(
            (self.application.output / SCHEDULER_STATUS_FILE).read_text(encoding="utf-8")
        )

    @property
    def events(self) -> tuple[SchedulerEvent, ...]:
        return tuple(
            SchedulerEvent.model_validate_json(line)
            for line in (self.application.output / SCHEDULER_EVENTS_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        )

    def pause(self, job: RunJobRecord) -> None:
        self.repository.request_pause(job.task_key, expected_revision=job.revision)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[asyncio.Task[None]]:
        task = asyncio.create_task(self.scheduler.run())
        try:
            yield task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.pool.aclose()


async def _wait_for(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", (20, 30))
async def test_run_slots_fill_and_backfill_without_exceeding_cap(
    tmp_path: Path, limit: int
) -> None:
    case = _Case(tmp_path, seeds=tuple(range(12)), max_runs=limit)
    async with case.running() as task:
        first = [await case.executor.next_started() for _ in range(limit)]
        assert len(case.executor.active) == limit
        assert [job.seed for job in first] == [seed for seed in range(12) for _ in range(3)][:limit]
        case.executor.release(first[0])
        replacement = await case.executor.next_started()
        assert replacement.seed == limit // 3
        assert len(case.executor.active) == limit
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)
    assert case.executor.peak == limit
    assert len(case.executor.started) == 36


@pytest.mark.asyncio
async def test_formal_matrix_starts_all_thirty_runs_in_seed_priority_order(
    tmp_path: Path,
) -> None:
    case = _Case(
        tmp_path,
        seeds=(11, 29, 47),
        max_runs=30,
        model_ids=tuple(f"model-{index}" for index in range(9)),
    )
    async with case.running() as task:
        first = [await case.executor.next_started() for _ in range(30)]
        assert [job.seed for job in first] == [11] * 10 + [29] * 10 + [47] * 10
        assert len(case.executor.active) == 30
        assert case.status.run_limit == case.status.max_runs == 30
        assert case.status.decision_limit is None
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)
    assert case.executor.peak == len(case.executor.started) == 30


@pytest.mark.asyncio
async def test_executor_exception_does_not_cancel_healthy_siblings(tmp_path: Path) -> None:
    case = _Case(tmp_path)
    attempts = 0

    async def execute(job: RunJobRecord, owner: str, admission: BoundedDecisionAdmission) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("isolated executor failure")
        await case.executor(job, owner, admission)

    case.scheduler._execute = execute
    async with case.running() as task:
        await case.executor.next_started()
        await case.executor.next_started()
        assert not case.executor.cancelled
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)
    assert attempts == len(case.jobs) + 1
    assert not case.executor.cancelled


@pytest.mark.asyncio
async def test_free_slot_backfills_next_seed_while_earlier_run_is_slow(tmp_path: Path) -> None:
    case = _Case(tmp_path, max_runs=2)
    async with case.running() as task:
        slow, fast = await case.executor.next_started(), await case.executor.next_started()
        assert (slow.seed, fast.seed) == (47, 47)
        case.executor.release(fast)
        last_earlier = await case.executor.next_started()
        assert last_earlier.seed == 47
        case.executor.release(last_earlier)
        later = await case.executor.next_started()
        assert later.seed == 11
        assert slow.task_key in case.executor.active
        assert len(case.executor.active) == 2
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)

    assert case.executor.peak == 2
    assert len({job.task_key for job in case.executor.started}) == len(case.jobs) == 9
    assert all(job.status is RunJobStatus.STOPPED for job in case.jobs)
    assert not case.executor.cancelled


@pytest.mark.asyncio
async def test_priority_follows_configured_seed_order_not_numeric_order(tmp_path: Path) -> None:
    case = _Case(tmp_path, max_runs=1)
    case.executor.drain()
    async with case.running() as task:
        await asyncio.wait_for(task, timeout=5)

    assert [job.seed for job in case.executor.started] == [47] * 3 + [11] * 3 + [29] * 3
    assert case.executor.peak == 1


@pytest.mark.asyncio
async def test_inherited_live_lease_reserves_capacity_until_its_run_finishes(
    tmp_path: Path,
) -> None:
    case = _Case(tmp_path, max_runs=2)
    inherited = case.repository.claim_next(
        case.application.plan.campaign_id,
        seed=47,
        lease_owner="previous-runner",
        lease_seconds=120,
    )
    assert inherited is not None

    async with case.running() as task:
        first = await case.executor.next_started()
        assert first.seed == 47
        assert case.status.active_runs == len(case.executor.started) == 1
        assert case.repository.get_job(inherited.task_key) == inherited
        case.repository.stop_run(
            inherited.task_key,
            lease_owner="previous-runner",
            expected_revision=inherited.revision,
            reason=RunStopReason.USER_REQUESTED,
            error="previous runner finished",
        )
        replacement = await case.executor.next_started()
        assert replacement.seed == 47
        assert first.task_key in case.executor.active
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)

    assert len(case.executor.started) == len(case.jobs) - 1
    assert inherited.task_key not in {job.task_key for job in case.executor.started}


@pytest.mark.asyncio
async def test_retry_cooldown_is_skipped_then_earlier_seed_regains_priority(tmp_path: Path) -> None:
    case = _Case(tmp_path, max_runs=1)
    earlier = [job for job in case.jobs if job.seed == 47]
    for job in earlier[1:]:
        case.pause(job)
    claimed = case.repository.claim_task(
        earlier[0].task_key, lease_owner="previous-runner", lease_seconds=120
    )
    assert claimed is not None
    retry = case.repository.interrupt_run(
        claimed.task_key,
        lease_owner="previous-runner",
        expected_revision=claimed.revision,
        error="temporary failure",
        retry_delay_seconds=0.5,
    )
    assert retry.retry_not_before is not None

    async with case.running() as task:
        later = await case.executor.next_started()
        assert later.seed == 11
        assert case.repository.get_job(retry.task_key) == retry
        await _wait_for(lambda: datetime.now(UTC) >= retry.retry_not_before)
        case.executor.release(later)
        resumed = await case.executor.next_started()
        assert resumed.task_key == retry.task_key
        assert resumed.attempt == 2
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["retry_cooldown", "live_lease"])
async def test_temporary_absence_of_eligible_jobs_does_not_finish_scheduler(
    tmp_path: Path, blocked_by: str
) -> None:
    case = _Case(tmp_path, seeds=(11,), max_runs=1)
    first, *others = case.jobs
    for job in others:
        case.pause(job)
    claimed = case.repository.claim_task(
        first.task_key,
        lease_owner="previous-runner",
        lease_seconds=0.2 if blocked_by == "live_lease" else 120,
    )
    assert claimed is not None
    if blocked_by == "retry_cooldown":
        case.repository.interrupt_run(
            first.task_key,
            lease_owner="previous-runner",
            expected_revision=claimed.revision,
            error="temporary failure",
            retry_delay_seconds=0.2,
        )

    async with case.running() as task:
        await _wait_for(lambda: case.probe.samples > 0)
        assert not task.done()
        assert not case.executor.started
        resumed = await case.executor.next_started()
        assert resumed.task_key == first.task_key
        assert resumed.attempt == 2
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_explicit_pause_and_stopped_runs_are_never_automatically_resumed(
    tmp_path: Path,
) -> None:
    case = _Case(tmp_path, seeds=(11,))
    queued, stopped, paused = case.jobs
    case.pause(queued)
    claimed = case.repository.claim_task(
        stopped.task_key, lease_owner="previous-runner", lease_seconds=120
    )
    assert claimed is not None
    case.repository.stop_run(
        stopped.task_key,
        lease_owner="previous-runner",
        expected_revision=claimed.revision,
        reason=RunStopReason.PROVIDER_RETRY_EXHAUSTED,
        error="provider attempts exhausted",
    )
    claimed = case.repository.claim_task(
        paused.task_key, lease_owner="previous-runner", lease_seconds=0.15
    )
    assert claimed is not None
    case.pause(claimed)

    async with case.running() as task:
        await asyncio.wait_for(task, timeout=5)

    assert not case.executor.started
    assert [job.attempt for job in case.jobs] == [0, 1, 1]
    assert all(job.status is RunJobStatus.STOPPED for job in case.jobs)
    assert [job.stop_reason for job in case.jobs] == [
        RunStopReason.USER_REQUESTED,
        RunStopReason.PROVIDER_RETRY_EXHAUSTED,
        RunStopReason.USER_REQUESTED,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["status", "events"])
async def test_optional_telemetry_io_failure_never_cancels_healthy_runs(
    tmp_path: Path, destination: str
) -> None:
    case = _Case(tmp_path)
    blocked = (
        case.scheduler._status.path if destination == "status" else case.scheduler._events_path
    )
    # A directory at the file path forces a real OSError at the selected write boundary.
    blocked.mkdir(parents=True)
    async with case.running() as task:
        first = await case.executor.next_started()
        await case.executor.next_started()
        assert len(case.executor.active) == 2
        case.executor.release(first)
        await case.executor.next_started()
        assert len(case.executor.active) == 2
        assert not case.executor.cancelled
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)
    assert len(case.executor.started) == len(case.jobs)
    assert case.executor.peak == 2
    assert not case.executor.cancelled
    assert case.scheduler.last_telemetry_error is not None


@pytest.mark.asyncio
async def test_resource_pressure_never_reduces_fixed_run_slots(tmp_path: Path) -> None:
    case = _Case(tmp_path, max_runs=3)
    async with case.running() as task:
        initial = [await case.executor.next_started() for _ in range(3)]
        case.probe.reading = case.probe.reading.model_copy(
            update={"cpu_percent": 99, "available_memory_mb": 1}
        )
        await _wait_for(lambda: case.probe.samples >= 3)
        assert case.status.run_limit == case.status.active_runs == 3
        assert case.status.allow_new_runs
        assert not case.executor.cancelled
        case.executor.release(initial[0])
        replacement = await case.executor.next_started()
        assert replacement.seed == 11
        assert len(case.executor.active) == 3
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)
    assert case.executor.peak == 3
    assert all(event.snapshot.adjustment == "monitoring" for event in case.events)


@pytest.mark.asyncio
async def test_unavailable_probe_does_not_block_admission(tmp_path: Path) -> None:
    case = _Case(tmp_path, max_runs=2)
    case.probe.reading = ResourceSample(probe_error="temporary measurement failure")
    async with case.running() as task:
        await case.executor.next_started()
        await case.executor.next_started()
        assert case.status.resource_status == "unavailable"
        assert case.status.allow_new_runs
        case.executor.drain()
        await asyncio.wait_for(task, timeout=5)
    assert len(case.executor.started) == len(case.jobs)
    assert not case.executor.cancelled


async def _hold_decision(case: _Case, identity: str, release: asyncio.Event) -> None:
    async with (
        case.scheduler.admission.admit(requires_model_slot=True),
        case.pool.lease(WorkerKey(owner_id=identity, company_id="company-1"), _Worker),
    ):
        await release.wait()


@pytest.mark.asyncio
async def test_unlimited_decisions_and_workers_are_observable_without_queueing(
    tmp_path: Path,
) -> None:
    case = _Case(tmp_path, max_runs=3)
    release = asyncio.Event()
    async with case.running() as task:
        for _ in range(3):
            await case.executor.next_started()
        holders = [
            asyncio.create_task(_hold_decision(case, str(index), release)) for index in range(40)
        ]
        try:
            await _wait_for(lambda: case.status.active_decisions == 40)
            assert case.scheduler.admission.limit is None
            assert case.pool.capacity is None
            assert case.status.decision_limit is None
            assert case.status.resident_workers == 40
            assert case.status.waiting_decisions == 0
            case.scheduler.observe_decision(DecisionWindowResult(status="sealed"), duration=0.1)
            await _wait_for(lambda: case.probe.samples >= 3)
            assert case.status.run_limit == 3
            release.set()
            await asyncio.gather(*holders)
            assert case.pool.resident_count == 40
            case.executor.drain()
            await asyncio.wait_for(task, timeout=5)
        finally:
            release.set()
            await asyncio.gather(*holders)
    assert case.executor.peak == 3
    assert not case.status.scheduler_running
    assert case.status.active_decisions == 0
    assert case.events[0].kind == "started" and case.events[-1].kind == "stopped"
    assert {event.plan_hash for event in case.events} == {case.application.plan.plan_hash}
