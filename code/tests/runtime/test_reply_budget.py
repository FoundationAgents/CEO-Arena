"""Failed attempts and automatic waiting share a durable finite decision allowance."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_runtime import InMemoryWorld, ScriptedHarness, _provider_run_config, _scenario

from ceo_arena.evaluation import ArenaAnalyzer, ArenaRunResult
from ceo_arena.execution import decision_status_by_company, recovery_by_company
from ceo_arena.models import (
    AgentBudget,
    AgentUsage,
    CompanyDecision,
    DecisionDiagnostics,
    DecisionWindowRequest,
    DecisionWindowResult,
    ProductDecision,
    ProviderFailure,
)
from ceo_arena.runtime.allowance import AllowanceLedger
from ceo_arena.runtime.context import CompanyContext
from ceo_arena.runtime.coordinator import (
    BenchmarkCoordinator,
    ProviderIdentityError,
    ProviderPermanentError,
)
from ceo_arena.runtime.retry import ProviderRetryPolicy


class ReportedFailureHarness(ScriptedHarness):
    def __init__(self, company_id: str, completed: set[str], spent_replies: int) -> None:
        super().__init__(company_id, completed, delay=0, price_cents=100)
        self.spent_replies = spent_replies
        self.allowances: list[int] = []
        self.error_reported = asyncio.Event()
        self.fail_once = True
        self.prior_usage: list[AgentUsage] = []

    @property
    def harness_id(self) -> str:
        return "opencode-native-test"

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        self.allowances.append(
            request.budget.max_provider_turns - request.prior_usage.provider_turns
        )
        self.prior_usage.append(request.prior_usage)
        if self.company_id == "c1" and len(self.allowances) == 1 and self.fail_once:
            self.error_reported.set()
            return DecisionWindowResult(
                status="provider_error",
                usage=AgentUsage(
                    provider_turns=self.spent_replies,
                    input_tokens=1000,
                    output_tokens=100,
                ),
                diagnostics=DecisionDiagnostics(protocol_errors=1),
                failure=ProviderFailure(category="worker", message="offline interrupted worker"),
            )
        return await super().decide(request, context)


def make_runtime(
    tmp_path: Path, spent: int
) -> tuple[BenchmarkCoordinator, dict[str, ReportedFailureHarness]]:
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    harnesses = {key: ReportedFailureHarness(key, completed, spent) for key in ("c1", "c2")}
    config = _provider_run_config(scenario, "allowance").model_copy(
        update={"budget": AgentBudget()}
    )
    runtime = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        retry_policy=ProviderRetryPolicy(initial_delay_seconds=0, maximum_delay_seconds=0),
    )
    return runtime, harnesses


@pytest.mark.asyncio
async def test_reported_worker_usage_is_accumulated_and_does_not_renew_32_replies(
    tmp_path: Path,
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 31)
    result = await runtime.run()
    assert result.final_state.day == 1
    assert harnesses["c1"].allowances == [32, 1]
    assert harnesses["c2"].allowances == [32]
    receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
    assert receipt.usage.provider_turns == 32
    assert receipt.usage.output_tokens == 110
    assert receipt.diagnostics.protocol_errors == 1 and receipt.diagnostics.recovered
    assert harnesses["c1"].prior_usage[1].output_tokens == 100


@pytest.mark.asyncio
async def test_infrastructure_failure_at_allowance_end_records_an_explicit_timeout(
    tmp_path: Path,
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 32)
    runtime.world.completed_harnesses.add("c1")
    result = await asyncio.wait_for(runtime.run(), 2)
    assert harnesses["c1"].allowances == [32]
    assert runtime.world.advance_calls == 1
    receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
    assert receipt.agent_status == "timeout"
    assert receipt.decision.products == ()
    assert receipt.failure and receipt.failure.category == "worker"
    assert receipt.failure.message == "offline interrupted worker"
    assert receipt.usage.provider_turns == 32
    assert not receipt.diagnostics.recovered
    assert receipt.error and "existing operating policy retained" in receipt.error
    status = json.loads((tmp_path / "performance_status.json").read_bytes())
    failure = status["companies"]["c1"]["failure"]
    assert failure["category"] == "worker"
    assert status["companies"]["c1"]["state"] == "timed_out"
    assert status["stage_count"]["provider_timeout"] == 1
    recovery = recovery_by_company(result.windows)
    assert recovery["c1"].infrastructure_timeout_count == 1
    assert recovery["c2"].infrastructure_timeout_count == 0
    arena_result = ArenaRunResult(
        run_id=result.run_id,
        lineup_id="offline-timeout-report",
        seed=11,
        assignments=runtime.config.assignments,
        official_score_by_company_cents={"c1": 123, "c2": 456},
        decision_status_by_company=decision_status_by_company(result.windows),
        recovery_by_company=recovery,
    )
    restored = ArenaRunResult.model_validate_json(arena_result.model_dump_json())
    model_id = runtime.config.assignments[0].model_id
    health = ArenaAnalyzer((restored,)).health(model_id)
    assert health.failure_count == 1 and health.status_counts == {"timeout": 1}
    assert health.recovery.infrastructure_timeout_count == 1
    assert health.recovery.recovered_windows == 0 and restored.model_score(model_id) == 123


@pytest.mark.asyncio
async def test_manual_restart_retains_reported_tokens_active_time_and_closing_replies(
    tmp_path: Path,
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 0)
    harnesses["c1"].fail_once = False
    usage = AgentUsage(
        provider_turns=30,
        finalization_turns=2,
        output_tokens=110_000,
        reasoning_tokens=100_000,
        latency_ms=2_800_000,
    )
    ledger = AllowanceLedger(tmp_path, runtime.config.config_hash, "c1", 0)
    ledger.save(usage, DecisionDiagnostics(protocol_errors=2))
    result = await runtime.run()
    assert harnesses["c1"].prior_usage == [usage]
    receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
    assert receipt.usage.output_tokens == 110_010
    assert receipt.usage.finalization_turns == 2
    assert receipt.usage.latency_ms >= 2_800_000
    assert runtime.config.budget.remaining_seconds(usage) == 800


def test_allowance_identity_and_all_hard_limits(tmp_path: Path) -> None:
    ledger = AllowanceLedger(tmp_path, "first-config", "c1", 0)
    ledger.save(AgentUsage(output_tokens=131_072), DecisionDiagnostics())
    with pytest.raises(ValueError, match="configuration"):
        AllowanceLedger(tmp_path, "different-config", "c1", 0).load()
    budget = AgentBudget()
    for usage in (
        AgentUsage(output_tokens=131_072),
        AgentUsage(latency_ms=3_600_000),
        AgentUsage(provider_turns=4, finalization_turns=4),
    ):
        assert budget.exhausted(usage)


@pytest.mark.asyncio
async def test_cancelled_retry_wait_is_charged_once_and_manual_pause_is_not(
    tmp_path: Path,
) -> None:
    ledger = AllowanceLedger(tmp_path, "same-config", "c1", 0)
    usage = AgentUsage(latency_ms=1200, provider_turns=2)
    diagnostics = DecisionDiagnostics(tool_errors=1)
    failure = ProviderFailure(category="http", http_status=503, message="temporarily unavailable")
    ledger.save(usage, diagnostics, failure)
    task = asyncio.create_task(ledger.wait(60, usage, diagnostics, failure))
    await asyncio.sleep(0.04)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    first = ledger.load()
    assert 1220 <= first.usage.latency_ms < 2000
    assert first.usage.provider_turns == 2 and first.failure == failure
    await asyncio.sleep(0.03)
    assert ledger.load() == first
    resumed = await ledger.wait(0.02, first.usage, first.diagnostics, first.failure)
    assert 10 <= resumed.latency_ms - first.usage.latency_ms < 500
    assert ledger.load().usage == resumed


@pytest.mark.asyncio
async def test_resume_exhausted_allowance_does_not_call_model_or_invent_original_failure(
    tmp_path: Path,
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 0)
    runtime.world.completed_harnesses.add("c1")
    ledger = AllowanceLedger(tmp_path, runtime.config.config_hash, "c1", 0)
    ledger.save(AgentUsage(latency_ms=3_600_000), DecisionDiagnostics())
    result = await asyncio.wait_for(runtime.run(), 2)
    receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
    assert harnesses["c1"].allowances == []
    assert receipt.agent_status == "timeout" and receipt.usage.latency_ms == 3_600_000
    assert receipt.failure and receipt.failure.category == "unknown"
    assert receipt.failure.reason == "recovery_allowance_exhausted"


@pytest.mark.asyncio
async def test_retry_after_is_capped_by_remaining_allowance_and_then_settles(
    tmp_path: Path,
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 0)
    runtime.config = runtime.config.model_copy(update={"budget": AgentBudget(timeout_seconds=0.06)})
    runtime._retry_policy = ProviderRetryPolicy(initial_delay_seconds=60)
    runtime.world.completed_harnesses.add("c1")
    result = await asyncio.wait_for(runtime.run(), 2)
    receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
    assert harnesses["c1"].allowances == [32]
    assert receipt.agent_status == "timeout"
    assert 60 <= receipt.usage.latency_ms < 1000
    assert harnesses["c2"].allowances == [32]


class RestoredTerminalHarness(ReportedFailureHarness):
    enforces_decision_budget = True

    def __init__(self, terminal_sealed: bool, failure: ProviderFailure | None = None) -> None:
        super().__init__("c1", {"c1"}, 0)
        self.terminal_sealed = terminal_sealed
        self.failure = failure

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        self.prior_usage.append(request.prior_usage)
        assert request.budget.exhausted(request.prior_usage)
        if self.failure:
            return DecisionWindowResult(status="provider_error", failure=self.failure)
        if not self.terminal_sealed:
            return DecisionWindowResult(status="budget_exhausted")
        return DecisionWindowResult(
            status="sealed",
            decision=CompanyDecision(
                company_id="c1",
                world_version=request.world_version,
                products=(ProductDecision(product_id="p1", price_cents=120),),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_sealed", [True, False])
async def test_exhausted_allowance_recovers_only_a_previously_sealed_worker_receipt(
    tmp_path: Path,
    terminal_sealed: bool,
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 0)
    terminal = RestoredTerminalHarness(terminal_sealed)
    runtime._harnesses["c1"] = terminal
    runtime.world.completed_harnesses.add("c1")
    usage = AgentUsage(provider_turns=32, latency_ms=3_600_000, output_tokens=131_072)
    failure = ProviderFailure(category="worker", message="terminal IPC reply lost")
    AllowanceLedger(tmp_path, runtime.config.config_hash, "c1", 0).save(
        usage,
        DecisionDiagnostics(),
        failure,
    )
    result = await asyncio.wait_for(runtime.run(), 2)
    receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
    assert terminal.prior_usage == [usage] and harnesses["c1"].allowances == []
    assert receipt.usage == usage
    if terminal_sealed:
        assert receipt.agent_status == "sealed" and receipt.failure is None
        assert receipt.decision.products[0].price_cents == 120
    else:
        assert receipt.agent_status == "timeout" and receipt.failure == failure
        assert receipt.decision.products == ()
    before = terminal.prior_usage.copy()
    await runtime.run()
    assert terminal.prior_usage == before  # The durable receipt is not restored twice.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (
            ProviderFailure(
                category="worker",
                reason="AgentPersistence",
                retryable=False,
                message="conversation journal is corrupt",
            ),
            ProviderPermanentError,
        ),
        (
            ProviderFailure(category="identity", retryable=False, message="wrong serving model"),
            ProviderIdentityError,
        ),
    ],
)
async def test_terminal_recovery_never_downgrades_a_new_permanent_fault_to_timeout(
    tmp_path: Path,
    failure: ProviderFailure,
    error_type: type[ProviderPermanentError],
) -> None:
    runtime, harnesses = make_runtime(tmp_path, 0)
    terminal = RestoredTerminalHarness(False, failure)
    runtime._harnesses["c1"] = terminal
    usage = AgentUsage(latency_ms=3_600_000)
    ledger = AllowanceLedger(tmp_path, runtime.config.config_hash, "c1", 0)
    ledger.save(
        usage,
        DecisionDiagnostics(),
        ProviderFailure(
            category="transport",
            message="original recoverable transport error",
        ),
    )
    for _ in range(2):
        with pytest.raises(error_type, match=failure.message):
            await asyncio.wait_for(runtime.run(), 2)
    assert terminal.prior_usage == [usage] and harnesses["c1"].allowances == []
    assert ledger.load().failure == failure and ledger.load().usage.latency_ms >= usage.latency_ms
    assert runtime.world.advance_calls == 0
    checkpoint = runtime.store.load_checkpoint(
        run_id=runtime.config.run_id,
        config_hash=runtime.config.config_hash,
    )
    assert checkpoint and checkpoint.current_state.day == 0 and checkpoint.collecting_window
    assert all(item.company_id != "c1" for item in checkpoint.collecting_window.decisions)
