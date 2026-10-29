"""End-to-end tests for the decision barrier, durability, query scope, and replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ceo_arena.economy import MarketWorld
from ceo_arena.harness import RULE_CEO_HARNESS_ID
from ceo_arena.models import (
    AgentBudget,
    AgentUsage,
    CapacityTierConfig,
    CompanyDayOutcome,
    CompanyDecision,
    CompanyObservation,
    CompanyState,
    DayResult,
    DecisionWindowRequest,
    DecisionWindowResult,
    InnovationMechanismConfig,
    MarketingMechanismConfig,
    OperationsMechanismConfig,
    ProductConfig,
    ProductDecision,
    ResearchMechanismConfig,
    ScenarioConfig,
    SegmentConfig,
    WindowResult,
    WorldState,
)
from ceo_arena.runtime.admission import BoundedDecisionAdmission
from ceo_arena.runtime.context import (
    CompanyContext,
    QueryRejectedError,
    StaleCompanyContextError,
    _issue_company_context,
)
from ceo_arena.runtime.coordinator import BenchmarkCoordinator, _cleanup_note
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.persistence import (
    AgentAssignment,
    CheckpointPhase,
    CollectingWindow,
    RunCheckpoint,
    RunConfig,
    RunManifest,
    SealedDecision,
)
from ceo_arena.runtime.replay import ExactReplay, ReplayMismatchError
from ceo_arena.runtime.trace import InMemoryTraceSink, JsonlTraceSink, TraceEvent, TraceKind


def _scenario(*, total_days: int = 2) -> ScenarioConfig:
    return ScenarioConfig(
        total_days=total_days,
        days_per_decision_window=1,
        initial_cash_cents=1_000,
        products=(
            ProductConfig(
                product_id="p1",
                base_quality=1.0,
                unit_cost_cents=50,
                salvage_value_cents=20,
                holding_cost_cents=1,
                procurement_lead_days=1,
                initial_inventory=10,
                initial_price_cents=100,
            ),
        ),
        segments=(
            SegmentConfig(
                segment_id="s1",
                base_daily_demand=10,
                price_reference_cents=100,
                price_weight=1,
                quality_weight=1,
                service_weight=1,
                awareness_weight=1,
                loyalty_weight=1,
                macro_sensitivity=0,
                seasonal_amplitude=0,
                seasonal_period_days=7,
                expected_quality=1,
                service_level_target=1,
            ),
        ),
        capacity_tiers=(
            CapacityTierConfig(
                tier=0,
                units_per_day=100,
                daily_cost_cents=0,
                expansion_cost_cents=0,
                expansion_lead_days=1,
            ),
        ),
        marketing=MarketingMechanismConfig(
            attention_power=0.5,
            outside_attention=1,
            goodwill_decay=0.1,
            awareness_gain=0.1,
            spend_reference_cents=100,
        ),
        innovation=InnovationMechanismConfig(
            daily_depreciation=0,
            development_delay_days=1,
            quality_scale=1,
            capital_reference_cents=100,
            project_cost_cents={1: 100, 2: 200, 3: 300},
            project_median_days={1: 1, 2: 2, 3: 3},
            project_capital_gain_cents={1: 100, 2: 200, 3: 300},
        ),
        operations=OperationsMechanismConfig(
            base_failure_probability=0,
            failure_probability_floor=0,
            reliability_spend_scale_cents=100,
            support_agent_daily_cost_cents=100,
            tickets_per_agent_day=1,
            base_ticket_rate=0,
            failure_ticket_rate=0,
            satisfaction_alpha=0.1,
            negative_experience_weight=1,
            abandonment_daily_rate=0,
        ),
        research=ResearchMechanismConfig(
            level_cost_cents={1: 10, 2: 20, 3: 30, 4: 40, 5: 50},
            level_delay_days={1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            level_signal_std={1: 1, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2},
            prior_std=1,
            weekly_process_std=0,
        ),
    )


def _company(company_id: str, scenario: ScenarioConfig) -> CompanyState:
    return CompanyState(
        company_id=company_id,
        cash_cents=scenario.initial_cash_cents,
        initial_cash_cents=scenario.initial_cash_cents,
        initial_enterprise_value_cents=scenario.initial_cash_cents,
        prices_cents={"p1": 100},
        promotions_cents={"p1": 0},
        reorder_targets={"p1": 10},
        inventory={"p1": 10},
        reserved_inventory={"p1": 0},
        goodwill={"s1": 0},
        awareness={"s1": 0},
        satisfaction={"s1": 0},
        loyalty={"s1": 0},
        development_budget_cents={"p1": 0},
        knowledge_capital_cents={"p1": 0},
    )


class InMemoryWorld:
    """Small deterministic functional MarketWorld used to verify runtime contracts."""

    def __init__(self, config: ScenarioConfig, completed_harnesses: set[str]) -> None:
        self.config = config
        self.completed_harnesses = completed_harnesses
        self.advance_calls = 0

    def create_initial_state(self, company_ids: Sequence[str]) -> WorldState:
        return WorldState(companies={item: _company(item, self.config) for item in company_ids})

    def project_observations(self, state: WorldState) -> dict[str, CompanyObservation]:
        prices = {
            company_id: dict(company.prices_cents)
            for company_id, company in state.companies.items()
        }
        return {
            company_id: CompanyObservation(
                scenario_id=state.scenario_id,
                world_version=state.version,
                day=state.day,
                total_days=self.config.total_days,
                days_remaining=max(0, self.config.total_days - state.day),
                days_per_decision_window=self.config.days_per_decision_window,
                company=company.model_copy(deep=True),
                public_prices_cents=prices,
                public_promotions_cents={
                    key: dict(value.promotions_cents) for key, value in state.companies.items()
                },
                public_effective_prices_cents={
                    key: {
                        product_id: price - value.promotions_cents[product_id]
                        for product_id, price in value.prices_cents.items()
                    }
                    for key, value in state.companies.items()
                },
                open_tenders=(),
                published_macro_index=state.macro_index,
            )
            for company_id, company in state.companies.items()
        }

    def advance_window(
        self,
        state: WorldState,
        decisions: Mapping[str, CompanyDecision] | Sequence[CompanyDecision],
    ) -> WindowResult:
        expected = set(state.companies)
        assert self.completed_harnesses == expected
        self.advance_calls += 1
        decision_map = (
            dict(decisions)
            if isinstance(decisions, Mapping)
            else {item.company_id: item for item in decisions}
        )
        companies = {key: value.model_copy(deep=True) for key, value in state.companies.items()}
        for company_id, decision in decision_map.items():
            gain = sum(item.price_cents or 0 for item in decision.products)
            companies[company_id].cash_cents += gain
        next_state = state.model_copy(
            update={
                "day": min(state.day + 1, self.config.total_days),
                "version": state.version + 1,
                "companies": companies,
            },
            deep=True,
        )
        day_hash = self.state_hash(next_state)
        day = DayResult(
            day=next_state.day,
            world_version=next_state.version,
            demand_by_segment={"s1": 0},
            company_outcomes={
                company_id: CompanyDayOutcome(
                    company_id=company_id,
                    consumer_orders=0,
                    fulfilled_units=0,
                    abandoned_units=0,
                    revenue_cents=0,
                    costs_cents=0,
                    ending_cash_cents=company.cash_cents,
                )
                for company_id, company in companies.items()
            },
            state_hash=day_hash,
        )
        return WindowResult(
            start_day=state.day,
            end_day=next_state.day,
            world_version=next_state.version,
            days=(day,),
            observations=self.project_observations(next_state),
            state=next_state,
        )

    def state_hash(self, state: WorldState) -> str:
        payload = json.dumps(
            state.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def validate_decision(self, state: WorldState, decision: CompanyDecision) -> None:
        if decision.company_id not in state.companies:
            raise ValueError("unknown company")
        if decision.world_version != state.version:
            raise ValueError("stale decision")


class ScriptedHarness:
    """Async Harness with deterministic decisions and configurable provider latency."""

    def __init__(
        self,
        company_id: str,
        completed: set[str],
        *,
        delay: float,
        price_cents: int,
        fail_if_called: bool = False,
    ) -> None:
        self.company_id = company_id
        self.completed = completed
        self.delay = delay
        self.price_cents = price_cents
        self.fail_if_called = fail_if_called
        self.call_count = 0
        self.observed_versions: list[int] = []

    @property
    def harness_id(self) -> str:
        return "scripted-v1"

    @property
    def model_id(self) -> str:
        return {"c1": "model-a", "c2": "model-b"}[self.company_id]

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        if self.fail_if_called:
            raise AssertionError("Harness must not be called while resuming sealed decisions")
        self.call_count += 1
        self.observed_versions.append(request.world_version)
        await asyncio.sleep(self.delay)
        query = context.query("SELECT company_id, world_version FROM company")
        assert query.rows == ((self.company_id, request.world_version),)
        self.completed.add(self.company_id)
        return DecisionWindowResult(
            status="sealed",
            decision=CompanyDecision(
                company_id=self.company_id,
                world_version=request.world_version,
                products=(ProductDecision(product_id="p1", price_cents=self.price_cents),),
            ),
            usage=AgentUsage(provider_turns=1, tool_calls=1, output_tokens=10),
        )

    async def cancel(self, company_id: str) -> None:
        assert company_id == self.company_id

    async def aclose(self) -> None:
        return None


class CleanupProbeHarness(ScriptedHarness):
    """Harness with controllable close and cancellation cleanup failures."""

    def __init__(
        self,
        company_id: str,
        completed: set[str],
        *,
        close_completed: asyncio.Event | None = None,
        close_error: Exception | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        super().__init__(company_id, completed, delay=0, price_cents=100)
        self._close_completed = close_completed
        self._close_error = close_error
        self._cancel_error = cancel_error
        self.started = asyncio.Event()

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        del request, context
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cancel(self, company_id: str) -> None:
        assert company_id == self.company_id
        if self._cancel_error is not None:
            raise self._cancel_error

    async def aclose(self) -> None:
        await asyncio.sleep(0.02)
        if self._close_completed is not None:
            self._close_completed.set()
        if self._close_error is not None:
            raise self._close_error


class ConcurrencyProbe:
    """Record active and peak admitted Harness calls on one event loop."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        self.active -= 1


class ProviderScriptedHarness(ScriptedHarness):
    """Scripted model Harness instrumented for cross-run concurrency tests."""

    def __init__(
        self,
        company_id: str,
        completed: set[str],
        *,
        delay: float,
        price_cents: int,
        probe: ConcurrencyProbe,
        fail_if_called: bool = False,
    ) -> None:
        super().__init__(
            company_id,
            completed,
            delay=delay,
            price_cents=price_cents,
            fail_if_called=fail_if_called,
        )
        self._probe = probe

    @property
    def harness_id(self) -> str:
        return "opencode-native-test"

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        self._probe.enter()
        try:
            return await super().decide(request, context)
        finally:
            self._probe.leave()


class RuleScriptedHarness(ScriptedHarness):
    """Scripted deterministic Harness carrying the non-provider Rule Harness identity."""

    @property
    def harness_id(self) -> str:
        return RULE_CEO_HARNESS_ID

    @property
    def model_id(self) -> str:
        return super().model_id


class InvalidEconomicHarness:
    """Seal a type-valid decision that the economic world must reject."""

    @property
    def harness_id(self) -> str:
        return "invalid-economic-v1"

    @property
    def model_id(self) -> str:
        return "model-a"

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        assert context.company_id == request.company_id
        return DecisionWindowResult(
            status="sealed",
            decision=CompanyDecision(
                company_id=request.company_id,
                world_version=request.world_version,
                capacity_target_tier=999,
            ),
            usage=AgentUsage(provider_turns=1, tool_calls=1),
        )

    async def cancel(self, company_id: str) -> None:
        del company_id

    async def aclose(self) -> None:
        return None


def _run_config(scenario: ScenarioConfig) -> RunConfig:
    return RunConfig(
        run_id="run-test",
        scenario=scenario,
        assignments=(
            AgentAssignment(
                company_id="c1",
                model_id="model-a",
                harness_id="scripted-v1",
            ),
            AgentAssignment(
                company_id="c2",
                model_id="model-b",
                harness_id="scripted-v1",
            ),
        ),
        budget=AgentBudget(
            max_provider_turns=2,
            max_tool_calls=2,
            max_output_tokens=100,
            timeout_seconds=1,
        ),
    )


def _provider_run_config(
    scenario: ScenarioConfig,
    run_id: str,
    *,
    timeout_seconds: float = 1,
) -> RunConfig:
    base = _run_config(scenario)
    return RunConfig(
        run_id=run_id,
        scenario=scenario,
        assignments=tuple(
            assignment.model_copy(
                update={
                    "harness_id": "opencode-native-test",
                    "api_index": api_index,
                }
            )
            for api_index, assignment in enumerate(base.assignments)
        ),
        budget=base.budget.model_copy(update={"timeout_seconds": timeout_seconds}),
        provider_endpoint_sha256="0" * 64,
    )


def _rule_run_config(scenario: ScenarioConfig) -> RunConfig:
    base = _run_config(scenario)
    return RunConfig(
        run_id="rule-run",
        scenario=scenario,
        assignments=tuple(
            assignment.model_copy(
                update={
                    "harness_id": RULE_CEO_HARNESS_ID,
                }
            )
            for assignment in base.assignments
        ),
        budget=base.budget,
    )


@pytest.mark.asyncio
async def test_barrier_waits_for_all_harnesses_and_advances_once(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    world = InMemoryWorld(scenario, completed)
    fast = ScriptedHarness("c1", completed, delay=0.01, price_cents=111)
    slow = ScriptedHarness("c2", completed, delay=0.05, price_cents=222)
    trace = InMemoryTraceSink()
    coordinator = BenchmarkCoordinator(
        _run_config(scenario),
        world,
        {"c1": fast, "c2": slow},
        tmp_path,
        trace_sink=trace,
    )

    result = await coordinator.run()

    assert world.advance_calls == 1
    assert fast.observed_versions == slow.observed_versions == [0]
    assert result.final_state.companies["c1"].cash_cents == 1_111
    assert result.final_state.companies["c2"].cash_cents == 1_222
    assert [record.kind for record in trace.records()] == [
        TraceKind.RUN_STARTED,
        TraceKind.DECISION_BARRIER_SEALED,
        TraceKind.WINDOW_COMMITTED,
        TraceKind.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_shared_admission_limits_model_calls_across_coordinators(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    admission = BoundedDecisionAdmission(max_inflight_model_decisions=1)
    probe = ConcurrencyProbe()

    def coordinator(run_id: str) -> BenchmarkCoordinator:
        completed: set[str] = set()
        harnesses = {
            company_id: ProviderScriptedHarness(
                company_id,
                completed,
                delay=0.02,
                price_cents=100,
                probe=probe,
            )
            for company_id in ("c1", "c2")
        }
        return BenchmarkCoordinator(
            _provider_run_config(scenario, run_id),
            InMemoryWorld(scenario, completed),
            harnesses,
            tmp_path / run_id,
            decision_admission=admission,
        )

    first, second = coordinator("run-a"), coordinator("run-b")
    await asyncio.gather(first.run(), second.run())

    assert probe.peak == 1
    assert probe.active == 0


@pytest.mark.asyncio
async def test_admission_queue_time_does_not_consume_harness_timeout(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    admission = BoundedDecisionAdmission(max_inflight_model_decisions=1)
    completed: set[str] = set()
    probe = ConcurrencyProbe()
    harnesses = {
        company_id: ProviderScriptedHarness(
            company_id,
            completed,
            delay=0,
            price_cents=100,
            probe=probe,
        )
        for company_id in ("c1", "c2")
    }
    coordinator = BenchmarkCoordinator(
        _provider_run_config(scenario, "queued-run", timeout_seconds=0.02),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        decision_admission=admission,
    )

    async with admission.admit(requires_model_slot=True):
        task = asyncio.create_task(coordinator.run())
        await asyncio.sleep(0.05)
        assert all(harness.call_count == 0 for harness in harnesses.values())
    result = await task

    decisions = result.windows[0].decisions
    assert all(decision.agent_status == "sealed" for decision in decisions)
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_rule_ceo_identity_bypasses_model_admission(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    admission = BoundedDecisionAdmission(max_inflight_model_decisions=1)
    completed: set[str] = set()
    harnesses = {
        company_id: RuleScriptedHarness(
            company_id,
            completed,
            delay=0,
            price_cents=100,
        )
        for company_id in ("c1", "c2")
    }
    coordinator = BenchmarkCoordinator(
        _rule_run_config(scenario),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        decision_admission=admission,
    )

    async with admission.admit(requires_model_slot=True):
        result = await asyncio.wait_for(coordinator.run(), timeout=0.5)

    assert all(decision.agent_status == "sealed" for decision in result.windows[0].decisions)


@pytest.mark.asyncio
async def test_bounded_admission_releases_permit_after_cancellation_and_error() -> None:
    admission = BoundedDecisionAdmission(max_inflight_model_decisions=1)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def occupy() -> None:
        async with admission.admit(requires_model_slot=True):
            entered.set()
            await blocked.wait()

    task = asyncio.create_task(occupy())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(RuntimeError, match="provider failed"):
        async with admission.admit(requires_model_slot=True):
            raise RuntimeError("provider failed")

    async with asyncio.timeout(0.5):
        async with admission.admit(requires_model_slot=True):
            pass


@pytest.mark.asyncio
async def test_coordinator_closes_all_harnesses_before_reporting_cleanup_failure(
    tmp_path: Path,
) -> None:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    healthy_closed = asyncio.Event()
    harnesses = {
        "c1": CleanupProbeHarness(
            "c1",
            completed,
            close_error=OSError("close failed"),
        ),
        "c2": CleanupProbeHarness(
            "c2",
            completed,
            close_completed=healthy_closed,
        ),
    }
    coordinator = BenchmarkCoordinator(
        _run_config(scenario),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
    )

    with pytest.raises(ExceptionGroup, match="failed to close"):
        await coordinator.aclose()

    assert healthy_closed.is_set()


@pytest.mark.asyncio
async def test_harness_cleanup_failure_does_not_replace_run_cancellation(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    harnesses = {
        company_id: CleanupProbeHarness(
            company_id,
            completed,
            cancel_error=OSError("Authorization: Bearer sk-cleanup-secret"),
        )
        for company_id in ("c1", "c2")
    }
    coordinator = BenchmarkCoordinator(
        _run_config(scenario),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
    )
    task = asyncio.create_task(coordinator.run())
    await asyncio.gather(*(harness.started.wait() for harness in harnesses.values()))
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_harness_cleanup_notes_redact_sensitive_values() -> None:
    note = _cleanup_note(
        "Harness cleanup failed",
        OSError("Authorization: Bearer sk-cleanup-secret"),
    )

    assert "sk-cleanup-secret" not in note
    assert "[REDACTED]" in note


def test_bounded_admission_requires_a_positive_integer_limit() -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        BoundedDecisionAdmission(True)
    with pytest.raises(ValueError, match="must be positive"):
        BoundedDecisionAdmission(0)


@pytest.mark.asyncio
async def test_resume_commits_collected_barrier_without_recalling_harness(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    first_world = InMemoryWorld(scenario, completed)
    initial_harnesses = {
        "c1": ScriptedHarness("c1", completed, delay=0, price_cents=111),
        "c2": ScriptedHarness("c2", completed, delay=0, price_cents=222),
    }
    first = BenchmarkCoordinator(config, first_world, initial_harnesses, tmp_path)
    checkpoint = first._open_run(resume=True)
    checkpoint = await first._collect_decision_barrier(checkpoint)
    assert checkpoint.phase is CheckpointPhase.COLLECTING_DECISIONS

    resumed_completed = {"c1", "c2"}
    resumed_world = InMemoryWorld(scenario, resumed_completed)
    forbidden = {
        "c1": ScriptedHarness("c1", resumed_completed, delay=0, price_cents=1, fail_if_called=True),
        "c2": ScriptedHarness("c2", resumed_completed, delay=0, price_cents=1, fail_if_called=True),
    }
    resumed = BenchmarkCoordinator(config, resumed_world, forbidden, tmp_path)
    result = await resumed.run(resume=True)

    assert resumed_world.advance_calls == 1
    assert all(harness.call_count == 0 for harness in forbidden.values())
    assert result.final_state.companies["c2"].cash_cents == 1_222


@pytest.mark.asyncio
async def test_resume_calls_only_companies_missing_from_partial_barrier(tmp_path: Path) -> None:
    class SimulatedCrash(RuntimeError):
        pass

    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    first_harnesses = {
        "c1": ScriptedHarness("c1", completed, delay=0, price_cents=111),
        "c2": ScriptedHarness("c2", completed, delay=0.2, price_cents=222),
    }
    first = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        first_harnesses,
        tmp_path,
    )
    checkpoint = first._open_run(resume=True)
    original_stage = first.store.stage_decision

    def stage_then_crash(
        *, run_id: str, config_hash: str, window: CollectingWindow, response: SealedDecision
    ) -> None:
        original_stage(run_id=run_id, config_hash=config_hash, window=window, response=response)
        if response.company_id == "c1":
            raise SimulatedCrash

    first.store.stage_decision = stage_then_crash  # type: ignore[method-assign]
    with pytest.raises(SimulatedCrash):
        await first._collect_decision_barrier(checkpoint)

    resumed_harnesses = {
        "c1": ScriptedHarness("c1", completed, delay=0, price_cents=1, fail_if_called=True),
        "c2": ScriptedHarness("c2", completed, delay=0, price_cents=222),
    }
    resumed = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        resumed_harnesses,
        tmp_path,
    )
    result = await resumed.run()

    assert result.final_state.companies["c1"].cash_cents == 1_111
    assert resumed_harnesses["c1"].call_count == 0
    assert resumed_harnesses["c2"].call_count == 1


@pytest.mark.asyncio
async def test_exact_replay_is_model_free_and_checks_daily_hashes(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=2)
    config = _run_config(scenario)
    world = InMemoryWorld(scenario, completed)
    harnesses = {
        "c1": ScriptedHarness("c1", completed, delay=0, price_cents=10),
        "c2": ScriptedHarness("c2", completed, delay=0, price_cents=20),
    }
    coordinator = BenchmarkCoordinator(config, world, harnesses, tmp_path)
    await coordinator.run()
    checkpoint = coordinator.store.load_checkpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
    )
    assert checkpoint is not None

    replay_world = InMemoryWorld(scenario, {"c1", "c2"})
    report = ExactReplay(replay_world).verify(checkpoint)
    assert report.verified_windows == 2
    assert report.verified_days == 2

    first_window = checkpoint.completed_windows[0]
    bad_fingerprint = first_window.day_fingerprints[0].model_copy(update={"state_hash": "wrong"})
    bad_window = first_window.model_copy(update={"day_fingerprints": (bad_fingerprint,)})
    bad_checkpoint = checkpoint.model_copy(
        update={"completed_windows": (bad_window, *checkpoint.completed_windows[1:])}
    )
    with pytest.raises(ReplayMismatchError, match="day 1 state hash"):
        ExactReplay(InMemoryWorld(scenario, {"c1", "c2"})).verify(bad_checkpoint)

    with pytest.raises(ValueError, match="complete checkpoint"):
        ExactReplay(replay_world).verify(
            checkpoint.model_copy(update={"phase": CheckpointPhase.READY})
        )


def test_exact_replay_rejects_nonterminal_complete_checkpoint() -> None:
    scenario = _scenario(total_days=2)
    world = InMemoryWorld(scenario, set())
    initial_state = world.create_initial_state(("c1", "c2"))
    early_checkpoint = RunCheckpoint(
        run_id="early-complete",
        config_hash="config",
        phase=CheckpointPhase.COMPLETE,
        initial_state=initial_state,
        current_state=initial_state,
        next_window_index=0,
    )

    with pytest.raises(ValueError, match="terminal world state"):
        ExactReplay(world).verify(early_checkpoint)

    future_state = initial_state.model_copy(update={"day": scenario.total_days + 1})
    future_checkpoint = early_checkpoint.model_copy(
        update={"initial_state": future_state, "current_state": future_state}
    )
    with pytest.raises(ValueError, match="terminal world state"):
        ExactReplay(world).verify(future_checkpoint)


@pytest.mark.asyncio
async def test_exact_replay_verifies_portable_harness_trace_receipts(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    completed: set[str] = set()
    coordinator = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        {
            "c1": ScriptedHarness("c1", completed, delay=0, price_cents=10),
            "c2": ScriptedHarness("c2", completed, delay=0, price_cents=20),
        },
        tmp_path,
    )
    await coordinator.run()
    checkpoint = coordinator.store.load_checkpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
    )
    assert checkpoint is not None
    trace_directory = tmp_path / "agent-traces"
    trace_directory.mkdir()
    trace_path = trace_directory / "receipt.ndjson"
    trace_path.write_text('{"kind":"test"}\n', encoding="utf-8")
    digest = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    window = checkpoint.completed_windows[0]
    sealed = window.decisions[0].model_copy(
        update={"harness_trace_path": "receipt.ndjson", "harness_trace_sha256": digest}
    )
    window = window.model_copy(update={"decisions": (sealed, *window.decisions[1:])})
    checkpoint = checkpoint.model_copy(update={"completed_windows": (window,)})
    replay = ExactReplay(InMemoryWorld(scenario, {"c1", "c2"}), artifact_directory=trace_directory)

    assert replay.verify(checkpoint).verified_days == 1
    trace_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ReplayMismatchError, match="trace hash mismatch"):
        replay.verify(checkpoint)


@pytest.mark.asyncio
async def test_exact_replay_rejects_semantically_forged_runtime_trace(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    completed: set[str] = set()
    coordinator = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        {
            "c1": ScriptedHarness("c1", completed, delay=0, price_cents=10),
            "c2": ScriptedHarness("c2", completed, delay=0, price_cents=20),
        },
        tmp_path,
    )
    await coordinator.run()
    checkpoint = coordinator.store.load_checkpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
    )
    assert checkpoint is not None
    trace_path = tmp_path / "trace.jsonl"
    original_trace = trace_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in original_trace.splitlines()]
    barrier = next(item for item in records if item["kind"] == "decision_barrier_sealed")
    barrier["decisions"][0]["decision"]["support_budget_cents"] = 12345
    trace_path.write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReplayMismatchError, match="runtime trace"):
        ExactReplay(
            InMemoryWorld(scenario, {"c1", "c2"}),
            runtime_trace_path=trace_path,
        ).verify(checkpoint)

    trace_path.write_text(original_trace + "INVALID-GARBAGE\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid trace record"):
        ExactReplay(
            InMemoryWorld(scenario, {"c1", "c2"}),
            runtime_trace_path=trace_path,
        ).verify(checkpoint)


def test_manifest_rejects_configuration_attribution_tampering() -> None:
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    state = InMemoryWorld(scenario, set()).create_initial_state(config.company_ids)
    manifest = RunManifest.create(config, InMemoryWorld(scenario, set()).state_hash(state))
    payload = manifest.model_dump(mode="json")
    payload["config"]["assignments"][0]["model_id"] = "forged-model"

    with pytest.raises(ValueError, match="configuration hash is invalid"):
        RunManifest.model_validate(payload)


def test_run_directory_lock_rejects_concurrent_owners(tmp_path: Path) -> None:
    path = tmp_path / ".run.lock"

    with (
        RunDirectoryLock(path),
        pytest.raises(RuntimeError, match="already active"),
        RunDirectoryLock(path),
    ):
        assert RunDirectoryLock.is_active(path)
        raise AssertionError("second owner unexpectedly acquired the run lock")
    assert not RunDirectoryLock.is_active(path)


def test_run_directory_lock_rejects_hard_link_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "important-data"
    path = tmp_path / ".run.lock"
    original = b"important-data-must-survive"
    target.write_bytes(original)
    os.link(target, path)

    with pytest.raises(RuntimeError, match="private regular file"), RunDirectoryLock(path):
        raise AssertionError("hard-linked run lock was unexpectedly accepted")

    assert target.read_bytes() == original


def test_run_directory_lock_does_not_trust_a_reused_live_pid(tmp_path: Path) -> None:
    path = tmp_path / ".run.lock"
    path.write_bytes(f'\0{{"pid":{os.getpid()}}}'.encode())

    assert not RunDirectoryLock.is_active(path)


@pytest.mark.asyncio
async def test_coordinator_refreshes_trace_after_a_failed_lock_attempt(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    stale_completed: set[str] = set()
    stale = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, stale_completed),
        {
            "c1": ScriptedHarness("c1", stale_completed, delay=0, price_cents=10),
            "c2": ScriptedHarness("c2", stale_completed, delay=0, price_cents=20),
        },
        tmp_path,
    )
    with RunDirectoryLock(tmp_path / ".run.lock"), pytest.raises(RuntimeError):
        await stale.run()

    fresh_completed: set[str] = set()
    fresh = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, fresh_completed),
        {
            "c1": ScriptedHarness("c1", fresh_completed, delay=0, price_cents=10),
            "c2": ScriptedHarness("c2", fresh_completed, delay=0, price_cents=20),
        },
        tmp_path,
    )
    await fresh.run()
    await stale.run()

    records = JsonlTraceSink(tmp_path / "trace.jsonl").records()
    assert tuple(item.sequence for item in records) == tuple(range(len(records)))


@pytest.mark.asyncio
async def test_coordinator_and_replay_integrate_with_real_market_world(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=2)
    config = _run_config(scenario)
    harnesses = {
        "c1": ScriptedHarness("c1", completed, delay=0, price_cents=100),
        "c2": ScriptedHarness("c2", completed, delay=0, price_cents=100),
    }
    coordinator = BenchmarkCoordinator(
        config,
        MarketWorld(scenario),
        harnesses,
        tmp_path,
    )

    result = await coordinator.run()
    checkpoint = coordinator.store.load_checkpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
    )

    assert result.final_state.day == 2
    assert checkpoint is not None
    report = ExactReplay(MarketWorld(scenario)).verify(checkpoint)
    assert report.verified_days == 2
    observation = MarketWorld(scenario).project_observations(result.final_state)["c1"]
    with _issue_company_context(observation) as context:
        assert context.query("SELECT COUNT(*) FROM daily_financials").rows == ((2,),)
        assert context.query("SELECT COUNT(*) FROM daily_product_sales").rows == ((2,),)
        assert context.query("SELECT COUNT(*) FROM daily_segment_service").rows == ((2,),)
        assert context.query("SELECT unit_cost_cents FROM product_catalog").rows == ((50,),)


@pytest.mark.asyncio
async def test_bankrupt_companies_are_not_called_in_later_windows(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=3)
    expensive_capacity = scenario.capacity_tiers[0].model_copy(update={"daily_cost_cents": 2_000})
    scenario = scenario.model_copy(update={"capacity_tiers": (expensive_capacity,)})
    config = _run_config(scenario)
    harnesses = {
        "c1": ScriptedHarness("c1", completed, delay=0, price_cents=100),
        "c2": ScriptedHarness("c2", completed, delay=0, price_cents=100),
    }
    coordinator = BenchmarkCoordinator(config, MarketWorld(scenario), harnesses, tmp_path)

    result = await coordinator.run()

    assert result.final_state.day == 1
    assert all(
        company.status.value == "bankrupt" for company in result.final_state.companies.values()
    )
    assert all(harness.call_count == 1 for harness in harnesses.values())
    checkpoint = coordinator.store.load_checkpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
    )
    assert checkpoint is not None
    assert ExactReplay(MarketWorld(scenario)).verify(checkpoint).verified_days == 1


@pytest.mark.asyncio
async def test_economically_invalid_decision_becomes_replayable_noop(tmp_path: Path) -> None:
    scenario = _scenario(total_days=1)
    assignment = AgentAssignment(
        company_id="c1",
        model_id="model-a",
        harness_id="invalid-economic-v1",
    )
    config = RunConfig(run_id="invalid-economic", scenario=scenario, assignments=(assignment,))
    coordinator = BenchmarkCoordinator(
        config,
        MarketWorld(scenario),
        {"c1": InvalidEconomicHarness()},
        tmp_path,
    )

    result = await coordinator.run()
    checkpoint = coordinator.store.load_checkpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
    )

    assert result.final_state.day == 1
    assert checkpoint is not None
    sealed = checkpoint.completed_windows[0].decisions[0]
    assert sealed.agent_status == "protocol_error"
    assert sealed.decision.capacity_target_tier is None
    assert ExactReplay(MarketWorld(scenario)).verify(checkpoint).verified_days == 1


def test_company_context_is_scoped_select_only_and_revocable() -> None:
    scenario = _scenario(total_days=1)
    world = InMemoryWorld(scenario, set())
    state = world.create_initial_state(("c1", "c2"))
    observation = world.project_observations(state)["c1"]
    context = _issue_company_context(observation)

    assert context.query("SELECT company_id, cash_cents FROM company").rows == (("c1", 1_000),)
    assert context.query(
        "SELECT reliability_budget_cents, support_budget_cents FROM company"
    ).rows == ((0, 0),)
    assert context.query(
        "SELECT company_id, product_id, price_cents FROM public_prices ORDER BY company_id"
    ).rows == (("c1", "p1", 100), ("c2", "p1", 100))
    assert context.query(
        "SELECT total_days, days_remaining, days_per_decision_window FROM scenario"
    ).rows == ((1, 1, 1),)
    assert context.query("SELECT effective_price_cents FROM public_prices").rows == (
        (100,),
        (100,),
    )
    assert context.query("SELECT x'FF'").rows == (("hex:ff",),)
    assert context.query("SELECT COUNT(*) FROM innovation_projects").rows == ((0,),)
    assert context.query("SELECT COUNT(*) FROM capacity_projects").rows == ((0,),)
    assert context.query("SELECT COUNT(*) FROM research_jobs").rows == ((0,),)
    with pytest.raises(QueryRejectedError):
        context.query("UPDATE company SET cash_cents = 0")
    with pytest.raises(QueryRejectedError):
        context.query("PRAGMA table_info(company)")
    with pytest.raises(QueryRejectedError):
        context.query("SELECT randomblob(1000000)")
    with pytest.raises(QueryRejectedError):
        context.query("SELECT random()")
    with pytest.raises(QueryRejectedError):
        context.query("SELECT datetime('now')")
    with pytest.raises(QueryRejectedError):
        context.query("SELECT CURRENT_TIMESTAMP")
    with pytest.raises(QueryRejectedError):
        context.query("SELECT printf('%1000000000s', 'x')")
    with pytest.raises(QueryRejectedError):
        context.query("SELECT 1", row_limit=10_001)
    with pytest.raises(TypeError):
        CompanyContext()  # type: ignore[call-arg]

    context._revoke()
    with pytest.raises(StaleCompanyContextError):
        context.query("SELECT * FROM company")


def test_jsonl_trace_is_append_only_idempotent_and_collision_safe(tmp_path: Path) -> None:
    from ceo_arena.runtime.trace import JsonlTraceSink

    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    event = TraceEvent(
        record_id="run:start",
        kind=TraceKind.RUN_STARTED,
        run_id="run",
        state_hash="abc",
    )
    first = sink.append(event)
    second = sink.append(event)
    assert first == second
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    with pytest.raises(ValueError, match="collision"):
        sink.append(event.model_copy(update={"state_hash": "different"}))
