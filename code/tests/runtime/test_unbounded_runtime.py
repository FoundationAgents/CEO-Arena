"""Unlimited logical decisions, isolated retries, durable receipts and safe telemetry."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Literal

import pytest
from test_runtime import (
    InMemoryWorld,
    ScriptedHarness,
    _provider_run_config,
    _run_config,
    _scenario,
)

from ceo_arena.execution import recovery_by_company
from ceo_arena.models import (
    AgentBudget,
    AgentUsage,
    CompanyDecision,
    DecisionDiagnostics,
    DecisionWindowRequest,
    DecisionWindowResult,
    ProviderFailure,
)
from ceo_arena.runtime.allowance import AllowanceLedger
from ceo_arena.runtime.context import CompanyContext
from ceo_arena.runtime.coordinator import (
    BenchmarkCoordinator,
    ProviderIdentityError,
    ProviderPermanentError,
)
from ceo_arena.runtime.performance import PerformanceRecorder
from ceo_arena.runtime.persistence import (
    AtomicCheckpointStore,
    RunCheckpoint,
)
from ceo_arena.runtime.retry import ProviderRetryPolicy


class RetryHarness(ScriptedHarness):
    def __init__(self, company_id: str, completed: set[str], failures: int | None) -> None:
        super().__init__(company_id, completed, delay=0, price_cents=100)
        self.failures = failures
        self.attempts = 0
        self.cancel_count = 0
        self.failed = asyncio.Event()

    @property
    def harness_id(self) -> str:
        return "opencode-native-test"

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        self.attempts += 1
        if self.failures is None or self.attempts <= self.failures:
            self.failed.set()
            await asyncio.sleep(0)
            return DecisionWindowResult(
                status="provider_error",
                error="upstream failed",
                failure=ProviderFailure(
                    category="http",
                    http_status=503,
                    message="Authorization: Bearer sk-offline-secret",
                ),
            )
        return await super().decide(request, context)

    async def cancel(self, company_id: str) -> None:
        await super().cancel(company_id)
        self.cancel_count += 1


def _retry_immediately() -> ProviderRetryPolicy:
    return ProviderRetryPolicy(initial_delay_seconds=0, maximum_delay_seconds=0)


class MismatchedProvider(RetryHarness):
    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        self.attempts += 1
        return DecisionWindowResult(
            status="provider_error",
            failure=ProviderFailure(
                category="identity",
                reason="ServingModelMismatch",
                retryable=False,
                message="expected model-a; received model-b",
            ),
        )


class BadRequestHarness(RetryHarness):
    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        result = await super().decide(request, context)
        return (
            result.model_copy(
                update={
                    "failure": result.failure.model_copy(
                        update={"http_status": 400, "retryable": False}
                    )
                }
            )
            if result.failure is not None
            else result
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [2, 3])
async def test_http400_attempts_three_times_without_interrupting_other_ceos(
    tmp_path: Path, failures: int
) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    config = _provider_run_config(scenario, "bounded400")
    harnesses = {
        "c1": BadRequestHarness("c1", completed, failures),
        "c2": RetryHarness("c2", completed, 0),
    }
    runtime = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        retry_policy=_retry_immediately(),
    )
    if failures == 2:
        result = await asyncio.wait_for(runtime.run(), 5)
        assert result.final_state.day == 1
        assert all(h.cancel_count == 0 for h in harnesses.values())
    else:
        with pytest.raises(ProviderPermanentError):
            await asyncio.wait_for(runtime.run(), 5)
        assert runtime.world.advance_calls == 0
        progress = AllowanceLedger(tmp_path, config.config_hash, "c1", 0).load()
        assert progress.failure is not None and progress.failure.attempt == 3
        with pytest.raises(ProviderPermanentError):
            await runtime.run()
    assert harnesses["c1"].attempts == 3
    assert harnesses["c2"].attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_attempts", [1, 2, 3, 4, 10])
async def test_http400_attempts_survive_resume_without_resetting_usage(
    tmp_path: Path, prior_attempts: int
) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    config = _provider_run_config(scenario, "resumed400")
    ledger = AllowanceLedger(tmp_path, config.config_hash, "c1", 0)
    ledger.save(
        AgentUsage(input_tokens=12345),
        DecisionDiagnostics(),
        ProviderFailure(
            category="http",
            http_status=400,
            attempt=prior_attempts,
            retryable=False,
            message="HTTP400",
        ),
    )
    harnesses = {
        "c1": BadRequestHarness("c1", completed, None),
        "c2": RetryHarness("c2", completed, 0),
    }
    runtime = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        retry_policy=_retry_immediately(),
    )
    with pytest.raises(ProviderPermanentError):
        await asyncio.wait_for(runtime.run(), 5)
    assert harnesses["c1"].attempts == max(0, 3 - prior_attempts)
    assert ledger.load().usage.input_tokens == 12345
    assert ledger.load().failure.attempt == max(3, prior_attempts)


def test_http400_successful_reply_resets_the_consecutive_counter() -> None:
    policy = ProviderRetryPolicy()
    previous = ProviderFailure(category="http", http_status=400, attempt=2, message="HTTP400")
    latest = previous.model_copy(update={"attempt": 1, "retryable": False})
    exhausted = policy.record_failure(latest, previous, made_progress=False)
    assert exhausted.attempt == 3 and not policy.can_retry(exhausted)
    restarted = policy.record_failure(latest, previous, made_progress=True)
    assert restarted.attempt == 1 and policy.can_retry(restarted)


@pytest.mark.parametrize("category", ["auth", "identity"])
def test_http400_does_not_retry_authentication_or_identity_failures(
    category: Literal["auth", "identity"],
) -> None:
    policy = ProviderRetryPolicy()
    failure = ProviderFailure(
        category=category, http_status=400, attempt=1, message="provider rejected request"
    )
    assert policy.record_failure(failure, None, made_progress=True) == failure
    assert not policy.can_retry(failure)
    with pytest.raises(ValueError, match="permanent"):
        policy.delay(1, failure)


def test_http400_default_backoff_is_one_then_two_seconds() -> None:
    policy = ProviderRetryPolicy()
    failure = ProviderFailure(category="http", http_status=400, message="HTTP400")
    for attempt in (1, 2):
        current = failure.model_copy(update={"attempt": attempt})
        assert policy.delay(attempt, current) == attempt
    with pytest.raises(ValueError, match="permanent"):
        policy.delay(3, failure.model_copy(update={"attempt": 3}))


@pytest.mark.asyncio
async def test_model_identity_mismatch_never_retries_or_advances_the_economy(
    tmp_path: Path,
) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    wrong = MismatchedProvider("c1", completed, None)
    runtime = BenchmarkCoordinator(
        _provider_run_config(scenario, "identity"),
        InMemoryWorld(scenario, completed),
        {"c1": wrong, "c2": RetryHarness("c2", completed, 0)},
        tmp_path,
        retry_policy=_retry_immediately(),
    )
    with pytest.raises(ProviderIdentityError, match="expected model-a"):
        await asyncio.wait_for(runtime.run(), 2)
    assert wrong.attempts == 1
    checkpoint = runtime.store.load_checkpoint(
        run_id=runtime.config.run_id, config_hash=runtime.config.config_hash
    )
    assert checkpoint is not None and checkpoint.current_state.day == 0


def test_default_decision_contract_has_finite_windows_and_uncapped_transport_attempts(
    tmp_path: Path,
) -> None:
    assert AgentBudget().model_dump() == {
        "max_provider_turns": 32,
        "max_tool_calls": None,
        "max_output_tokens": 131_072,
        "timeout_seconds": 3_600,
    }
    scenario = _scenario(total_days=1)
    completed: set[str] = set()
    config = _run_config(scenario).model_copy(update={"budget": AgentBudget()})
    harnesses = {
        key: ScriptedHarness(key, completed, delay=0, price_cents=100) for key in ("c1", "c2")
    }
    runtime = BenchmarkCoordinator(config, InMemoryWorld(scenario, completed), harnesses, tmp_path)
    result = DecisionWindowResult(
        status="sealed",
        decision=CompanyDecision(company_id="c1", world_version=0),
        usage=AgentUsage(provider_turns=32, tool_calls=1000, output_tokens=99999),
    )
    assert runtime._validate_harness_result("c1", 0, result).status == "sealed"
    runtime.config = config.model_copy(update={"budget": AgentBudget(max_output_tokens=10)})
    assert runtime._validate_harness_result("c1", 0, result).error == "output token budget exceeded"


@pytest.mark.parametrize("status", [401, 402, 403, 404, 422, 501, 505])
def test_http_permanent_failure_never_gets_a_retry_delay(status: int) -> None:
    failure = ProviderFailure(category="http", http_status=status, message="bad request")
    policy = ProviderRetryPolicy()
    assert not policy.can_retry(failure)
    with pytest.raises(ValueError, match="permanent"):
        policy.delay(1, failure)


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
def test_transient_http_retry_honors_retry_after(status: int) -> None:
    failure = ProviderFailure(
        category="http", http_status=status, message="retry later", retry_after_seconds=80
    )
    policy = ProviderRetryPolicy()
    assert policy.can_retry(failure) and policy.delay(1, failure) == 80


@pytest.mark.asyncio
async def test_permanent_failure_is_not_reissued_after_resume(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    config = _provider_run_config(scenario, "bad-request")
    harnesses = {key: RetryHarness(key, completed, 0) for key in ("c1", "c2")}
    runtime = BenchmarkCoordinator(config, InMemoryWorld(scenario, completed), harnesses, tmp_path)
    AllowanceLedger(tmp_path, config.config_hash, "c1", 0).save(
        AgentUsage(provider_turns=1),
        DecisionDiagnostics(),
        ProviderFailure(
            category="http", http_status=400, attempt=10, message="invalid conversation"
        ),
    )
    with pytest.raises(ProviderPermanentError, match="invalid conversation"):
        await runtime.run()
    assert harnesses["c1"].attempts == 0 and runtime.world.advance_calls == 0


@pytest.mark.asyncio
async def test_one_ceo_retries_beyond_thirty_without_restarting_healthy_company(
    tmp_path: Path,
) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    harnesses = {"c1": RetryHarness("c1", completed, 35), "c2": RetryHarness("c2", completed, 0)}
    runtime = BenchmarkCoordinator(
        _provider_run_config(scenario, "isolated"),
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        retry_policy=_retry_immediately(),
    )
    result = await asyncio.wait_for(runtime.run(), 5)
    assert result.final_state.day == 1
    assert harnesses["c1"].attempts == 36
    assert harnesses["c2"].attempts == 1
    assert all(harness.cancel_count == 0 for harness in harnesses.values())
    events = [
        json.loads(line) for line in (tmp_path / "performance.jsonl").read_text().splitlines()
    ]
    retries = [event for event in events if event["stage"] == "provider_retry"]
    assert len(retries) == 35
    assert all(event["failure"]["http_status"] == 503 for event in retries)
    assert all(event["duration_ms"] > 0 for event in retries)
    assert "sk-offline-secret" not in (tmp_path / "performance.jsonl").read_text()
    status = json.loads((tmp_path / "performance_status.json").read_text())
    assert status["day"] == status["total_days"] == 1
    assert status["phase"] == "complete"
    assert status["config_hash"] == runtime.config.config_hash
    assert status["stage_count"]["decision_stage"] == 2
    # Initial, collecting, sealed, terminal committed: no per-CEO full snapshot writes.
    assert status["stage_count"]["checkpoint"] == 4


@pytest.mark.asyncio
async def test_pause_interrupts_retry_wait_and_keeps_healthy_receipt_in_journal(
    tmp_path: Path,
) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    harnesses = {"c1": RetryHarness("c1", completed, None), "c2": RetryHarness("c2", completed, 0)}
    config = _provider_run_config(scenario, "pause")
    runtime = BenchmarkCoordinator(
        config,
        InMemoryWorld(scenario, completed),
        harnesses,
        tmp_path,
        retry_policy=ProviderRetryPolicy(initial_delay_seconds=60),
    )
    task = asyncio.create_task(runtime.run())
    try:
        async with asyncio.timeout(2):
            while True:
                checkpoint = runtime.store.load_checkpoint(
                    run_id=config.run_id, config_hash=config.config_hash
                )
                if (
                    checkpoint
                    and checkpoint.collecting_window
                    and checkpoint.collecting_window.decisions
                ):
                    break
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert harnesses["c1"].attempts == 1
        assert harnesses["c2"].attempts == 1
        assert harnesses["c2"].cancel_count == 0
        snapshot = RunCheckpoint.model_validate_json((tmp_path / "checkpoint.json").read_bytes())
        assert snapshot.collecting_window and not snapshot.collecting_window.decisions
        restored = AtomicCheckpointStore(tmp_path / "checkpoint.json").load(
            run_id=config.run_id, config_hash=config.config_hash
        )
        assert restored and restored.collecting_window
        assert [item.company_id for item in restored.collecting_window.decisions] == ["c2"]
        allowance = AllowanceLedger(tmp_path, config.config_hash, "c1", 0).load()
        assert allowance.usage.latency_ms > 1 and allowance.failure
        harnesses["c1"].failures = 0
        result = await runtime.run()
        assert result.final_state.day == 1
        assert harnesses["c1"].attempts == 2 and harnesses["c2"].attempts == 1
        receipt = next(item for item in result.windows[0].decisions if item.company_id == "c1")
        assert receipt.usage.latency_ms >= allowance.usage.latency_ms
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_finite_timeout_retains_actual_elapsed_time(tmp_path: Path) -> None:
    completed = {"c1", "c2"}
    scenario = _scenario(total_days=1)
    harnesses = {
        key: ScriptedHarness(key, completed, delay=0.1, price_cents=100) for key in completed
    }
    config = _run_config(scenario).model_copy(update={"budget": AgentBudget(timeout_seconds=0.02)})
    result = await BenchmarkCoordinator(
        config, InMemoryWorld(scenario, completed), harnesses, tmp_path
    ).run()
    assert all(
        item.agent_status == "timeout" and item.usage.latency_ms >= 10
        for item in result.windows[0].decisions
    )
    assert all(
        item.infrastructure_timeout_count == 0
        for item in recovery_by_company(result.windows).values()
    )


def test_optional_telemetry_failure_cannot_mask_the_underlying_failure(tmp_path: Path) -> None:
    recorder = PerformanceRecorder(tmp_path)
    recorder.path.mkdir()  # Writing this optional JSONL file now raises IsADirectoryError.
    recorder.record(
        "provider_retry",
        1,
        failure=ProviderFailure(category="http", message="upstream", http_status=503),
    )
    assert recorder.last_error is not None
    with pytest.raises(ValueError, match="original"), recorder.measure("harness"):
        raise ValueError("original")


@pytest.mark.asyncio
async def test_journal_is_idempotent_full_durable_and_rejects_window_drift(tmp_path: Path) -> None:
    completed: set[str] = set()
    scenario = _scenario(total_days=1)
    config = _run_config(scenario)
    harnesses = {
        key: ScriptedHarness(key, completed, delay=0, price_cents=100) for key in ("c1", "c2")
    }
    runtime = BenchmarkCoordinator(config, InMemoryWorld(scenario, completed), harnesses, tmp_path)
    initial = runtime._open_run(resume=False)
    checkpoint = await runtime._collect_decision_barrier(initial)
    assert checkpoint.collecting_window
    window = checkpoint.collecting_window
    store = runtime.store.checkpoints
    assert isinstance(store, AtomicCheckpointStore)
    before = (tmp_path / "checkpoint.json").read_bytes()
    response = window.decisions[0]
    store.stage_decision(
        run_id=config.run_id, config_hash=config.config_hash, window=window, response=response
    )
    assert (tmp_path / "checkpoint.json").read_bytes() == before
    with sqlite3.connect(store.journal_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM received_decisions").fetchone() == (2,)
    with store._journal(write=True) as connection:
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
    for changed in (
        window.model_copy(update={"start_state_hash": "wrong"}),
        window.model_copy(update={"start_world_version": 100}),
    ):
        with pytest.raises(ValueError, match="current collecting"):
            store.stage_decision(
                run_id=config.run_id,
                config_hash=config.config_hash,
                window=changed,
                response=response,
            )
    with pytest.raises(ValueError, match="immutable"):
        store.stage_decision(
            run_id=config.run_id,
            config_hash=config.config_hash,
            window=window,
            response=response.model_copy(update={"error": "changed"}),
        )
