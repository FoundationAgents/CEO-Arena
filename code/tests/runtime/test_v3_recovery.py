from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.execution import recovery_by_company
from ceo_arena.harness.draft import AgentToolGateway, DecisionDraftError, DecisionSealedError
from ceo_arena.models import (
    CapacityPolicyConfig,
    CompanyDecision,
    DecisionDiagnostics,
    DecisionWindowRequest,
    DecisionWindowResult,
    MarketDynamicsConfig,
    MarketPhaseConfig,
    ProductDecision,
    ScenarioConfig,
)
from ceo_arena.runtime.context import (
    CompanyContext,
    DecisionRejectedError,
    StaleCompanyContextError,
    _issue_company_context,
)
from ceo_arena.runtime.coordinator import BenchmarkCoordinator
from ceo_arena.runtime.persistence import (
    AgentAssignment,
    AtomicCheckpointStore,
    CheckpointPhase,
    RunCheckpoint,
    RunConfig,
)
from ceo_arena.runtime.replay import ExactReplay
from ceo_arena.runtime.trace import JsonlTraceSink


class LifecycleHarness:
    """Exercise a corrected draft, cancellation and downsize through the issued capability."""

    harness_id = "capacity-lifecycle-test"

    def __init__(self, company_id: str) -> None:
        self.company_id = company_id
        self.model_id = f"script-{company_id}"
        self.called_days: list[int] = []

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        day = int(context.query("SELECT day FROM company").rows[0][0])
        self.called_days.append(day)
        gateway = AgentToolGateway(request.observation)
        diagnostics = DecisionDiagnostics()
        if self.company_id == "focal":
            if day in (0, 2):
                gateway.expand_capacity(1)
            elif day == 1:
                row = context.query(
                    "SELECT project_id FROM capacity_projects WHERE status='pending'"
                ).rows[0]
                gateway.cancel_capacity_project(str(row[0]))
                before = gateway.draft.preview().model_dump()
                with pytest.raises(DecisionRejectedError, match="unknown capacity tier"):
                    context.validate_decision(
                        CompanyDecision(
                            company_id=self.company_id,
                            world_version=request.world_version,
                            capacity_target_tier=999,
                        )
                    )
                assert gateway.draft.preview().model_dump() == before
                diagnostics = DecisionDiagnostics(tool_errors=1, recovered=True)
            elif day == 7:
                gateway.downsize_capacity(0)
        assert gateway.preview().affordable
        context.validate_decision(gateway.draft.preview())
        return DecisionWindowResult(
            status="sealed", decision=gateway.seal(), diagnostics=diagnostics
        )


class SimulatedCrash(RuntimeError):
    pass


class CrashAfterSave(AtomicCheckpointStore):
    """Crash after durable publication, on either side of cancellation settlement."""

    def __init__(self, path: Path, phase: CheckpointPhase) -> None:
        super().__init__(path)
        self.phase = phase
        self.crashed = False

    def save(self, checkpoint: RunCheckpoint) -> None:
        super().save(checkpoint)
        target_day = 1 if self.phase is CheckpointPhase.DECISIONS_SEALED else 2
        if (
            not self.crashed
            and checkpoint.phase is self.phase
            and checkpoint.current_state.day == target_day
        ):
            self.crashed = True
            raise SimulatedCrash("interrupted after durable checkpoint")


def _scenario() -> ScenarioConfig:
    scenario = default_scenario(total_days=14)
    tiers = tuple(
        tier.model_copy(update={"expansion_lead_days": 4}) for tier in scenario.capacity_tiers
    )
    return scenario.model_copy(
        update={
            "days_per_decision_window": 1,
            "capacity_tiers": tiers,
            "capacity_policy": CapacityPolicyConfig(downsize_lead_days=2),
            "market_dynamics": MarketDynamicsConfig(
                phases=(
                    MarketPhaseConfig(phase_id="value", demand_multiplier=0.9),
                    MarketPhaseConfig(phase_id="quality", demand_multiplier=1.1),
                ),
                transitions=((0.6, 0.4), (0.4, 0.6)),
                publication_delay_days=2,
            ),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [CheckpointPhase.DECISIONS_SEALED, CheckpointPhase.READY])
async def test_capacity_refund_and_market_path_survive_checkpoint_recovery(
    tmp_path: Path,
    phase: CheckpointPhase,
) -> None:
    scenario = _scenario()
    harnesses = {name: LifecycleHarness(name) for name in ("focal", "other")}
    config = RunConfig(
        run_id="capacity-recovery",
        scenario=scenario,
        assignments=tuple(
            AgentAssignment(
                company_id=name, model_id=harness.model_id, harness_id=harness.harness_id
            )
            for name, harness in harnesses.items()
        ),
    )
    world = MarketWorld(scenario)
    uninterrupted = await BenchmarkCoordinator(
        config,
        world,
        harnesses,
        tmp_path / "baseline",
    ).run()
    directory = tmp_path / "resumed"
    crashed = BenchmarkCoordinator(
        config,
        MarketWorld(scenario),
        {name: LifecycleHarness(name) for name in harnesses},
        directory,
        checkpoint_store=CrashAfterSave(directory / "checkpoint.json", phase),
    )
    with pytest.raises(SimulatedCrash):
        await crashed.run()
    resumed_harnesses = {name: LifecycleHarness(name) for name in harnesses}
    resumed = BenchmarkCoordinator(config, MarketWorld(scenario), resumed_harnesses, directory)
    result = await resumed.run()

    assert world.state_hash(result.final_state) == world.state_hash(uninterrupted.final_state)
    assert result.final_state.market_signals == uninterrupted.final_state.market_signals
    company = result.final_state.companies["focal"]
    assert company.capacity_tier == 0
    assert company.cumulative_refunds_cents > 0
    assert (
        sum(record.refunds_cents for record in company.daily_history)
        == company.cumulative_refunds_cents
    )
    assert sum(receipt.refunded_cents > 0 for receipt in company.action_receipts) == 1
    assert sum(project.refund_cents > 0 for project in company.capacity_projects) == 1
    assert all(day >= 2 for harness in resumed_harnesses.values() for day in harness.called_days)
    for record in company.daily_history:
        assert record.expense_breakdown.total == record.costs_cents
    checkpoint = resumed.store.load_checkpoint(run_id=config.run_id, config_hash=config.config_hash)
    assert checkpoint is not None
    saved = checkpoint.completed_windows[1].decisions
    corrected = next(decision for decision in saved if decision.company_id == "focal")
    assert corrected.diagnostics == DecisionDiagnostics(tool_errors=1, recovered=True)
    assert recovery_by_company(checkpoint.completed_windows)["focal"].recovery_rate == 1.0
    traced = [
        decision
        for record in JsonlTraceSink(directory / "trace.jsonl").records()
        for decision in record.decisions
        if decision.company_id == "focal" and decision.diagnostics.recovered
    ]
    assert len(traced) == 1
    assert traced[0].diagnostics == corrected.diagnostics
    assert (
        ExactReplay(MarketWorld(scenario)).verify(checkpoint).verified_days == scenario.total_days
    )
    completed_again = await resumed.run()
    assert world.state_hash(completed_again.final_state) == world.state_hash(result.final_state)


def test_refund_preview_failed_validation_and_reset_leave_world_and_random_path_unchanged() -> None:
    scenario = _scenario()
    world = MarketWorld(scenario)
    state = world.create_initial_state(("focal",))
    state = world.advance_window(
        state,
        {"focal": CompanyDecision(company_id="focal", world_version=0, capacity_target_tier=1)},
    ).state
    state.companies["focal"].cash_cents = 100
    pristine = state.detached_copy()
    original_hash = world.state_hash(state)
    observation = world.project_observations(state)["focal"]
    original_observation = observation.model_dump()
    gateway = AgentToolGateway(observation)
    project_id = observation.company.capacity_projects[0].project_id
    gateway.cancel_capacity_project(project_id)
    gateway.draft.start_research_project(3)
    unsealed = gateway.draft.preview().model_dump()

    with _issue_company_context(observation, partial(world.validate_decision, state)) as context:
        assert context.query("SELECT SUM(refunds_cents) FROM daily_cashflow").rows == ((0,),)
        for _ in range(2):
            assert gateway.preview().upfront_refunds_cents > 0
            assert not gateway.preview().affordable
            with pytest.raises(DecisionRejectedError, match="exceed available cash"):
                context.validate_decision(gateway.draft.preview())
            with pytest.raises(DecisionDraftError, match="exceed available cash"):
                gateway.seal()
            assert not gateway.draft.sealed
            assert gateway.draft.preview().model_dump() == unsealed
            assert world.state_hash(state) == original_hash
        gateway.draft.reset()
        assert gateway.preview().upfront_refunds_cents == 0
        gateway.cancel_capacity_project(project_id)
        gateway.set_product(ProductDecision(product_id="A", price_cents=4_500))
        context.validate_decision(gateway.draft.preview())
        decision = gateway.seal()
        sealed = decision.model_dump()
        with pytest.raises(DecisionSealedError):
            gateway.draft.reset()
        assert gateway.draft.preview().model_dump() == sealed
    with pytest.raises(StaleCompanyContextError):
        context.validate_decision(decision)
    assert observation.model_dump() == original_observation
    assert world.state_hash(state) == original_hash
    actual = world.advance_window(state, {"focal": decision}).state
    reference = MarketWorld(scenario).advance_window(pristine, {"focal": decision}).state
    assert world.state_hash(actual) == world.state_hash(reference)
    assert actual.market_signals == reference.market_signals
    assert actual.companies["focal"].daily_history[-1].refunds_cents > 0
