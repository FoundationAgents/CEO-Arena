import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from factories import request
from pydantic import SecretStr

from ceo_arena.harness import AgentHarness, OpenCodeHarness, OpenCodeHarnessConfig
from ceo_arena.harness.wire import WorkerDecisionRequest, WorkerDecisionResponse
from ceo_arena.inference import InferenceSettings, resolve_inference
from ceo_arena.models import AgentBudget, DecisionWindowResult, ProviderFailure
from ceo_arena.runtime.context import _issue_company_context
from ceo_arena.runtime.lock import _process_exists

ROOT = Path(__file__).resolve().parents[2]
FAKE_WORKER = ROOT / "tests" / "harness" / "fake_worker.py"


def config(model: str = "fake") -> OpenCodeHarnessConfig:
    return OpenCodeHarnessConfig(
        worker_command=(sys.executable, "-u", str(FAKE_WORKER)),
        worker_directory=ROOT,
        base_url="https://not-used.invalid/v1",
        model=model,
        api_key=SecretStr("sk-test-secret"),
    )


def test_config_rejects_key_management_url() -> None:
    with pytest.raises(ValueError, match="key-management"):
        OpenCodeHarnessConfig(
            worker_directory=ROOT,
            base_url="https://newapi.example/keys",
            model="model-a",
            api_key=SecretStr("test-secret"),
        )


@pytest.mark.asyncio
async def test_internal_identity_mismatch_is_not_a_model_protocol_error() -> None:
    harness = OpenCodeHarness(config())
    decision_request = request().model_copy(update={"company_id": "wrong-company"})
    try:
        with _issue_company_context(decision_request.observation) as context:
            result = await harness.decide(decision_request, context)
        assert result.status == "provider_error"
        assert not harness._workers
    finally:
        await harness.aclose()


@pytest.mark.asyncio
async def test_rejects_workers_with_a_different_protocol() -> None:
    harness = OpenCodeHarness(config("legacy"))
    decision_request = request()
    try:
        with _issue_company_context(decision_request.observation) as context:
            result = await harness.decide(decision_request, context)
        assert result.status == "provider_error"
        assert result.decision is None
    finally:
        await harness.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [None, 5])
async def test_adapter_routes_context_queries_and_returns_bound_decision(
    timeout_seconds: float | None,
) -> None:
    harness = OpenCodeHarness(config())
    decision_request = request().model_copy(
        update={"budget": AgentBudget(timeout_seconds=timeout_seconds)}
    )
    try:
        with _issue_company_context(decision_request.observation) as context:
            result = await harness.decide(decision_request, context)
    finally:
        await harness.aclose()

    assert isinstance(harness, AgentHarness)
    assert result.status == "sealed"
    assert result.decision is not None
    assert result.decision.company_id == decision_request.company_id
    assert result.decision.world_version == decision_request.world_version


@pytest.mark.asyncio
async def test_adapter_redacts_worker_stderr_on_infrastructure_failure() -> None:
    harness = OpenCodeHarness(config(model="fail"))
    decision_request = request()
    try:
        with _issue_company_context(decision_request.observation) as context:
            result = await harness.decide(decision_request, context)
    finally:
        await harness.aclose()

    assert result.status == "provider_error"
    assert "sk-test-secret" not in (result.error or "")
    assert "[REDACTED]" in (result.error or "")
    assert result.failure is not None
    assert result.failure.category == "worker"
    assert "sk-test-secret" not in result.failure.message


@pytest.mark.asyncio
async def test_adapter_kills_worker_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ceo_arena.harness.opencode._WORKER_REPORT_GRACE_SECONDS", 0.05)
    harness = OpenCodeHarness(config(model="slow"))
    decision_request = request().model_copy(update={"budget": AgentBudget(timeout_seconds=1)})
    try:
        with _issue_company_context(decision_request.observation) as context:
            task = asyncio.create_task(harness.decide(decision_request, context))
            process = None
            for _ in range(50):
                worker = harness._workers.get(decision_request.company_id)
                process = worker._process if worker is not None else None
                if process is not None:
                    break
                await asyncio.sleep(0.02)
            assert process is not None
            pid = process.pid
            result = await task
    finally:
        await harness.aclose()

    assert result.status == "provider_error"
    assert "deadline" in (result.error or "")
    for _ in range(50):
        if not _process_exists(pid):
            break
        await asyncio.sleep(0.02)
    assert not _process_exists(pid)


def test_worker_wire_preserves_unbounded_limits_and_structured_failure() -> None:
    decision_request = request().model_copy(update={"budget": AgentBudget()})
    message = WorkerDecisionRequest(request_id="offline", request=decision_request)
    assert (
        WorkerDecisionRequest.model_validate_json(message.model_dump_json()).request.budget
        == AgentBudget()
    )
    assert message.request.budget.max_provider_turns == 32
    assert message.request.budget.max_output_tokens == 131_072
    assert message.request.budget.timeout_seconds == 3_600
    assert message.request.budget.max_tool_calls is None
    failure = ProviderFailure(
        category="stream_decode",
        message="Invalid provider stream",
        retryable=False,
        reason="InvalidProviderOutput",
        frame_sha256="f" * 64,
        frame_bytes=120,
    )
    response = WorkerDecisionResponse(
        request_id="offline",
        result=DecisionWindowResult(status="provider_error", failure=failure),
    )
    assert (
        WorkerDecisionResponse.model_validate_json(response.model_dump_json()).result.failure
        == failure
    )


@pytest.mark.asyncio
async def test_worker_receives_only_allowlisted_host_environment(monkeypatch) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-worker")
    harness = OpenCodeHarness(config(model="env-leak"))
    decision_request = request()
    try:
        with _issue_company_context(decision_request.observation) as context:
            result = await harness.decide(decision_request, context)
    finally:
        await harness.aclose()

    assert result.status == "sealed"


@pytest.mark.asyncio
async def test_worker_receives_the_exact_secret_free_inference_settings(monkeypatch) -> None:
    spawn = AsyncMock(wraps=asyncio.create_subprocess_exec)
    monkeypatch.setattr("ceo_arena.harness.opencode.asyncio.create_subprocess_exec", spawn)
    settings = resolve_inference("gpt-5.6-sol").model_copy(update={"effort": "high"})
    harness = OpenCodeHarness(config("gpt-5.6-sol").model_copy(update={"inference": settings}))
    assert harness.inference_settings == settings
    decision_request = request()
    try:
        with _issue_company_context(decision_request.observation) as context:
            result = await harness.decide(decision_request, context)
        wire = spawn.call_args.kwargs["env"]["CEO_ARENA_INFERENCE"]
    finally:
        await harness.aclose()

    assert result.status == "sealed"
    assert InferenceSettings.model_validate_json(wire) == settings
    assert "sk-test-secret" not in wire


@pytest.mark.asyncio
async def test_adapter_closes_all_workers_before_reporting_one_cleanup_failure() -> None:
    harness = OpenCodeHarness(config())
    failed = AsyncMock()
    failed.close.side_effect = OSError("api_key=sk-test-secret")
    completed = asyncio.Event()
    healthy = AsyncMock()

    async def close_healthy() -> None:
        await asyncio.sleep(0.02)
        completed.set()

    healthy.close.side_effect = close_healthy
    harness._workers = {"failed": failed, "healthy": healthy}

    with pytest.raises(ExceptionGroup, match="failed to close") as cleanup:
        await harness.aclose()

    details = " ".join(str(error) for error in cleanup.value.exceptions)
    assert completed.is_set()
    assert "sk-test-secret" not in details
    assert "[REDACTED]" in details
    failed.kill.assert_awaited_once()
    healthy.kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_finishes_worker_cleanup_before_propagating_cancellation() -> None:
    harness = OpenCodeHarness(config())
    completed = asyncio.Event()
    worker = AsyncMock()

    async def close_worker() -> None:
        await asyncio.sleep(0.05)
        completed.set()

    worker.close.side_effect = close_worker
    harness._workers = {"worker": worker}
    task = asyncio.create_task(harness.aclose())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed.is_set()


@pytest.mark.asyncio
async def test_adapter_retains_worker_when_forced_cancel_cleanup_fails() -> None:
    harness = OpenCodeHarness(config())
    worker = AsyncMock()
    worker.kill.side_effect = OSError("kill failed")
    harness._workers = {"worker": worker}

    with pytest.raises(OSError, match="kill failed"):
        await harness.cancel("worker")

    assert harness._workers["worker"] is worker
