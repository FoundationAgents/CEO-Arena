from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from factories import request
from pydantic import SecretStr

from ceo_arena.harness.opencode import OpenCodeHarness, OpenCodeHarnessConfig, _CompanyWorker
from ceo_arena.harness.worker_pool import WorkerKey, WorkerPool
from ceo_arena.models import DecisionWindowRequest, DecisionWindowResult
from ceo_arena.runtime.context import CompanyContext, _issue_company_context
from ceo_arena.runtime.lock import _process_exists

ROOT = Path(__file__).resolve().parents[2]
FAKE_WORKER = ROOT / "tests" / "harness" / "fake_worker.py"


@dataclass
class WorkerProbe:
    resident: int = 0
    peak: int = 0
    workers: list[StubWorker] = field(default_factory=list)

    def create(self) -> StubWorker:
        worker = StubWorker(self)
        self.workers.append(worker)
        self.resident += 1
        self.peak = max(self.peak, self.resident)
        return worker


class StubWorker:
    def __init__(self, probe: WorkerProbe) -> None:
        self.probe = probe
        self.closed = False
        self.killed = False
        self.closing = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_close.set()

    async def close(self) -> None:
        self.closing.set()
        await self.allow_close.wait()
        await asyncio.sleep(0)
        if not self.closed:
            self.closed = True
            self.probe.resident -= 1

    async def kill(self) -> None:
        self.killed = True
        await self.close()


@pytest.mark.asyncio
async def test_unlimited_pool_reuses_all_healthy_workers_until_owner_retirement() -> None:
    probe = WorkerProbe()
    pool: WorkerPool[StubWorker] = WorkerPool()
    keys = [WorkerKey(owner_id="one-run", company_id=str(index)) for index in range(40)]
    for key in keys:
        async with pool.lease(key, probe.create):
            pass
    assert pool.capacity is pool.max_idle is None
    assert pool.resident_count == len(probe.workers) == 40
    for key, original in zip(keys, probe.workers, strict=True):
        async with pool.lease(key, probe.create) as lease:
            assert lease.worker is original
    assert len(probe.workers) == 40
    await pool.trim()
    assert pool.resident_count == 40
    await pool.retire("one-run")
    assert pool.resident_count == probe.resident == 0


class StubCompanyWorker(StubWorker):
    probe: WorkerProbe

    def __init__(self, company_id: str, config: OpenCodeHarnessConfig) -> None:
        super().__init__(type(self).probe)
        self.company_id = company_id
        self.config = config
        self.probe.workers.append(self)
        self.probe.resident += 1
        self.probe.peak = max(self.probe.peak, self.probe.resident)

    async def decide(
        self, decision_request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        await asyncio.sleep(0)
        return DecisionWindowResult(status="protocol_error", error="stub result")


def worker_key(company_id: str, owner_id: str = "run-a") -> WorkerKey:
    return WorkerKey(owner_id=owner_id, company_id=company_id)


def worker_config(model: str = "fake", trace: Path | None = None) -> OpenCodeHarnessConfig:
    return OpenCodeHarnessConfig(
        worker_command=(sys.executable, "-u", str(FAKE_WORKER)),
        worker_directory=ROOT,
        base_url="https://not-used.invalid/v1",
        model=model,
        api_key=SecretStr(f"sk-{model}"),
        trace_directory=trace,
        verify_provenance=False,
    )


@pytest.mark.asyncio
async def test_pool_hard_cap_includes_workers_still_closing() -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=2, max_idle=0)

    async def use_worker(index: int) -> None:
        async with pool.lease(worker_key(str(index)), probe.create):
            assert probe.resident <= 2
            await asyncio.sleep(0.005)

    await asyncio.gather(*(use_worker(index) for index in range(12)))
    assert probe.peak == 2
    assert probe.resident == pool.resident_count == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_cancelled_lease_reaps_before_slot_reuse_despite_repeated_cancellation() -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=1, max_idle=1)
    started = asyncio.Event()

    async def hold() -> None:
        async with pool.lease(worker_key("first"), probe.create) as lease:
            lease.worker.allow_close.clear()
            started.set()
            await asyncio.Event().wait()

    async def acquire_next() -> None:
        async with pool.lease(worker_key("next"), probe.create):
            assert probe.workers[0].closed

    task = asyncio.create_task(hold())
    await started.wait()
    first = probe.workers[0]
    task.cancel()
    await first.closing.wait()
    waiting = asyncio.create_task(acquire_next())
    task.cancel()
    await asyncio.sleep(0.01)
    assert len(probe.workers) == pool.resident_count == 1
    assert not task.done() and not waiting.done()
    first.allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await waiting
    assert first.killed
    assert probe.peak == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_cancelled_pool_waiter_leaves_no_reserved_slot() -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=1, max_idle=0)

    async def wait_for_worker() -> None:
        async with pool.lease(worker_key("waiting"), probe.create):
            pytest.fail("waiter must not acquire while the only slot is occupied")

    async with pool.lease(worker_key("held"), probe.create):
        waiting = asyncio.create_task(wait_for_worker())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert len(probe.workers) == 1
    assert pool.resident_count == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_reuses_exact_identity_and_evicts_least_recently_used_idle_worker() -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=2, max_idle=2)
    async with pool.lease(worker_key("a"), probe.create) as first:
        original = first.worker
    async with pool.lease(worker_key("b"), probe.create) as second:
        old = second.worker
    async with pool.lease(worker_key("a"), probe.create) as repeated:
        assert repeated.worker is original
    async with pool.lease(worker_key("c"), probe.create):
        assert old.closed and not original.closed
    assert probe.peak == 2
    await pool.aclose()
    assert probe.resident == 0


@pytest.mark.asyncio
async def test_idle_cache_retains_most_recent_worker_and_resize_reaps_surplus() -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=2, max_idle=1)
    async with (
        pool.lease(worker_key("a"), probe.create) as first,
        pool.lease(worker_key("b"), probe.create) as second,
    ):
        pass
    assert second.worker.closed and not first.worker.closed
    async with pool.lease(worker_key("b"), probe.create):
        await pool.resize(1)
        assert first.worker.closed
        assert pool.resident_count == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_harness_scope_excludes_pool_wait_from_outer_decision_timeout(monkeypatch) -> None:
    probe = WorkerProbe()
    monkeypatch.setattr(StubCompanyWorker, "probe", probe, raising=False)
    monkeypatch.setattr("ceo_arena.harness.opencode._CompanyWorker", StubCompanyWorker)
    pool = WorkerPool[StubCompanyWorker](capacity=1, max_idle=0)
    harness = OpenCodeHarness(worker_config(), worker_pool=pool)
    decision_request = request()
    entered = asyncio.Event()

    async def decide() -> DecisionWindowResult:
        entered.set()
        async with harness.decision_scope(decision_request.company_id):
            with _issue_company_context(decision_request.observation) as context:
                return await asyncio.wait_for(harness.decide(decision_request, context), 0.05)

    async with pool.lease(worker_key("held"), lambda: StubCompanyWorker("held", worker_config())):
        task = asyncio.create_task(decide())
        await entered.wait()
        await asyncio.sleep(0.08)
        assert not task.done()
    result = await task
    assert result.error == "stub result"
    assert len(probe.workers) == 2
    assert probe.workers[-1].killed
    await harness.aclose()
    await pool.aclose()


@pytest.mark.asyncio
async def test_failed_reaping_keeps_slot_reserved_until_cleanup_retry_succeeds(monkeypatch) -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=1, max_idle=1)
    async with pool.lease(worker_key("failed"), probe.create) as lease:
        worker = lease.worker

    async def fail_cleanup() -> None:
        raise OSError("simulated process still alive")

    original_close = worker.close
    original_kill = worker.kill
    monkeypatch.setattr(worker, "close", fail_cleanup)
    monkeypatch.setattr(worker, "kill", fail_cleanup)
    with pytest.raises(ExceptionGroup, match="failed to close"):
        await pool.retire("run-a")
    assert probe.resident == pool.resident_count == 1
    assert not worker.closed
    monkeypatch.setattr(worker, "close", original_close)
    monkeypatch.setattr(worker, "kill", original_kill)
    await pool.aclose()
    assert probe.resident == pool.resident_count == 0


@pytest.mark.asyncio
async def test_pool_close_waits_for_active_owner_but_reaps_its_idle_workers() -> None:
    probe = WorkerProbe()
    pool = WorkerPool[StubWorker](capacity=2, max_idle=2)
    async with pool.lease(worker_key("idle", "run-b"), probe.create) as idle:
        pass
    async with pool.lease(worker_key("active"), probe.create) as active:
        closing = asyncio.create_task(pool.aclose())
        await idle.worker.closing.wait()
        assert not closing.done()
        assert not active.worker.closed
    await closing
    assert probe.resident == pool.resident_count == 0


@pytest.mark.asyncio
async def test_same_company_in_different_runs_never_shares_model_key_or_trace(
    monkeypatch, tmp_path
) -> None:
    probe = WorkerProbe()
    monkeypatch.setattr(StubCompanyWorker, "probe", probe, raising=False)
    monkeypatch.setattr("ceo_arena.harness.opencode._CompanyWorker", StubCompanyWorker)
    pool = WorkerPool[StubCompanyWorker](capacity=2, max_idle=2)
    config_a = worker_config("model-a", tmp_path / "run-a")
    first = OpenCodeHarness(config_a, worker_pool=pool)
    second = OpenCodeHarness(worker_config("model-b", tmp_path / "run-b"), worker_pool=pool)
    config_a.model = "mutated-after-construction"
    async with first.decision_scope("company-1"):
        worker_a = first._workers["company-1"]
    async with second.decision_scope("company-1"):
        worker_b = second._workers["company-1"]
    assert worker_a is not worker_b
    assert worker_a.config.model == "model-a"
    assert worker_b.config.model == "model-b"
    assert worker_a.config.api_key != worker_b.config.api_key
    assert worker_a.config.trace_directory != worker_b.config.trace_directory
    await first.aclose()
    assert worker_a.closed and not worker_b.closed
    assert pool.resident_count == 1
    await second.aclose()
    assert pool.resident_count == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_cancellation_during_subprocess_creation_claims_and_reaps_owned_process(
    monkeypatch,
) -> None:
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    allow_return = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def delayed_spawn(*args, **kwargs) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        if args[0] == sys.executable:
            processes.append(process)
            spawned.set()
            await allow_return.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    worker = _CompanyWorker("company-1", worker_config())
    starting = asyncio.create_task(worker.start())
    await asyncio.wait_for(spawned.wait(), 5)
    starting.cancel()
    await asyncio.sleep(0.01)
    assert not starting.done()
    allow_return.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert worker._process is None
    assert processes[0].returncode is not None
    assert not _process_exists(processes[0].pid)
