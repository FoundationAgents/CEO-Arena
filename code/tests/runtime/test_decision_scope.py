"""Coordinator contracts for worker acquisition and adaptive decision samples."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_runtime import (
    ConcurrencyProbe,
    InMemoryWorld,
    ProviderScriptedHarness,
    RuleScriptedHarness,
    _provider_run_config,
    _rule_run_config,
    _scenario,
)

from ceo_arena.models import CompanyObservation, DecisionWindowRequest, DecisionWindowResult
from ceo_arena.runtime import coordinator as coordinator_module
from ceo_arena.runtime.admission import BoundedDecisionAdmission
from ceo_arena.runtime.context import (
    CompanyContext,
    StaleCompanyContextError,
    _issue_company_context,
)
from ceo_arena.runtime.coordinator import BenchmarkCoordinator


@dataclass
class DecisionClock:
    now: float = 0

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now


@dataclass
class DecisionSamples:
    results: list[DecisionWindowResult] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)

    def observe(self, result: DecisionWindowResult, duration: float) -> None:
        self.results.append(result)
        self.durations.append(duration)


class ScopedHarness(ProviderScriptedHarness):
    def __init__(
        self,
        company_id: str,
        completed: set[str],
        admission: BoundedDecisionAdmission,
        clock: DecisionClock | None = None,
    ) -> None:
        super().__init__(company_id, completed, delay=0, price_cents=100, probe=ConcurrencyProbe())
        self.admission = admission
        self.clock = clock
        self.waiting = asyncio.Event()
        self.ready = asyncio.Event()
        self.exited = asyncio.Event()
        self.scoped = False

    @asynccontextmanager
    async def decision_scope(self, company_id: str) -> AsyncIterator[None]:
        assert company_id == self.company_id
        assert self.admission.active_count > 0
        self.waiting.set()
        try:
            await self.ready.wait()
            if self.clock is not None:
                self.clock.now += 100
            self.scoped = True
            yield
        finally:
            self.scoped = False
            self.exited.set()

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        assert self.scoped
        result = await super().decide(request, context)
        if self.clock is not None:
            self.clock.now += 3
        return result


@pytest.mark.asyncio
async def test_worker_scope_queue_does_not_consume_decision_timeout_or_issue_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    admission = BoundedDecisionAdmission(2)
    harnesses = {
        company_id: ScopedHarness(company_id, completed, admission) for company_id in ("c1", "c2")
    }
    issued: list[CompanyContext] = []

    def issue_context(
        observation: CompanyObservation, validator=None, timing=None
    ) -> CompanyContext:
        assert harnesses[observation.company.company_id].scoped
        context = _issue_company_context(observation, validator, timing)
        issued.append(context)
        return context

    monkeypatch.setattr(coordinator_module, "_issue_company_context", issue_context)
    coordinator = BenchmarkCoordinator(
        _provider_run_config(scenario, "scope-queue", timeout_seconds=0.02),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        decision_admission=admission,
    )
    task = asyncio.create_task(coordinator.run())
    try:
        await asyncio.wait_for(
            asyncio.gather(*(harness.waiting.wait() for harness in harnesses.values())),
            timeout=1,
        )
        await asyncio.sleep(0.06)
        assert not task.done()
        assert all(harness.call_count == 0 for harness in harnesses.values())
        assert issued == []
        for harness in harnesses.values():
            harness.ready.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert all(item.agent_status == "sealed" for item in result.windows[0].decisions)
        assert all(harness.exited.is_set() for harness in harnesses.values())
        assert admission.active_count == admission.waiting_count == 0
        assert len(issued) == 2
        for context in issued:
            with pytest.raises(StaleCompanyContextError):
                context.query("SELECT company_id FROM company")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_decision_observer_excludes_admission_and_scope_queue_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    admission = BoundedDecisionAdmission(1)
    clock = DecisionClock()
    samples = DecisionSamples()
    monkeypatch.setattr(coordinator_module, "time", clock)
    harnesses = {
        company_id: ScopedHarness(company_id, completed, admission, clock)
        for company_id in ("c1", "c2")
    }
    for harness in harnesses.values():
        harness.ready.set()
    coordinator = BenchmarkCoordinator(
        _provider_run_config(scenario, "observer-queue"),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        decision_admission=admission,
        decision_observer=samples.observe,
    )
    async with admission.admit(requires_model_slot=True):
        task = asyncio.create_task(coordinator.run())
        async with asyncio.timeout(1):
            while admission.waiting_count != 2:
                await asyncio.sleep(0)
        clock.now += 1_000
        assert all(not harness.waiting.is_set() for harness in harnesses.values())
        assert samples.results == []
    try:
        result = await asyncio.wait_for(task, timeout=1)
        assert all(item.agent_status == "sealed" for item in result.windows[0].decisions)
        assert samples.durations == [3, 3]
        assert all(item.status == "sealed" for item in samples.results)
        assert {item.decision.company_id for item in samples.results if item.decision} == {
            "c1",
            "c2",
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_scope_cancellation_releases_admission_without_sampling(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    admission = BoundedDecisionAdmission(1)
    samples = DecisionSamples()
    harnesses = {
        company_id: ScopedHarness(company_id, completed, admission) for company_id in ("c1", "c2")
    }
    coordinator = BenchmarkCoordinator(
        _provider_run_config(scenario, "cancel-scope"),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        decision_admission=admission,
        decision_observer=samples.observe,
    )
    task = asyncio.create_task(coordinator.run())
    try:
        await asyncio.wait_for(harnesses["c1"].waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert harnesses["c1"].exited.is_set()
        assert admission.active_count == admission.waiting_count == 0
        assert samples.results == []
        assert completed == set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_rule_decisions_do_not_produce_adaptive_model_samples(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    admission = BoundedDecisionAdmission(1)
    samples = DecisionSamples()
    coordinator = BenchmarkCoordinator(
        _rule_run_config(scenario),
        InMemoryWorld(scenario, completed),
        {
            company_id: RuleScriptedHarness(company_id, completed, delay=0, price_cents=100)
            for company_id in ("c1", "c2")
        },
        tmp_path,
        decision_admission=admission,
        decision_observer=samples.observe,
    )
    async with admission.admit(requires_model_slot=True):
        result = await asyncio.wait_for(coordinator.run(), timeout=1)
        assert admission.active_count == 1
        assert admission.waiting_count == 0
    assert all(item.agent_status == "sealed" for item in result.windows[0].decisions)
    assert samples.results == samples.durations == []
