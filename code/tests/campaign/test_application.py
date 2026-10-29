from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from ceo_arena.campaign.application import (
    CampaignApplication,
    _deterministic_retry_backoff,
    campaign_report,
)
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.models import RULE_MODEL_ID
from ceo_arena.campaign.records import (
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.campaign.repository import (
    RepositoryConflictError,
)
from ceo_arena.campaign.resources import SchedulerSnapshot
from ceo_arena.campaign.routes import ModelRouteRegistry, RuntimeModelRoute
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.harness import OPENCODE_LLM_COMMIT
from ceo_arena.harness.opencode import OpenCodeHarness, OpenCodeHarnessConfig
from ceo_arena.harness.worker_pool import WorkerPool
from ceo_arena.models import (
    AgentUsage,
    CompanyDecision,
    DecisionWindowRequest,
    DecisionWindowResult,
    ProviderFailure,
)
from ceo_arena.runtime import BoundedDecisionAdmission, CompanyContext
from ceo_arena.runtime.retry import ProviderRetryPolicy


class _ModelHarness:
    harness_id = f"opencode-native-{OPENCODE_LLM_COMMIT[:12]}"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        del context
        return DecisionWindowResult(
            status="sealed",
            decision=CompanyDecision(
                company_id=request.company_id,
                world_version=request.world_version,
            ),
        )

    async def cancel(self, company_id: str) -> None:
        del company_id


class _FailingHarness(_ModelHarness):
    def __init__(
        self, model_id: str, failed: asyncio.Event, failure: ProviderFailure | None = None
    ) -> None:
        super().__init__(model_id)
        self.failed = failed
        self.failure = failure or ProviderFailure(
            category="auth",
            http_status=401,
            retryable=False,
            message="offline authentication failure",
        )

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        del request, context
        self.failed.set()
        return DecisionWindowResult(
            status="provider_error",
            failure=self.failure,
        )


class _ExhaustedHarness(_ModelHarness):
    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        return DecisionWindowResult(
            status="budget_exhausted",
            usage=AgentUsage(provider_turns=request.budget.max_provider_turns),
            error="offline model used its decision replies without sealing",
        )


class _RecoveringHarness(_ModelHarness):
    """Retry beyond the old outer attempt count, then await a test-controlled recovery."""

    def __init__(self, model_id: str, ready: asyncio.Event, recovered: asyncio.Event) -> None:
        super().__init__(model_id)
        self.ready = ready
        self.recovered = recovered
        self.calls = 0

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        self.calls += 1
        if self.calls <= 4:
            return DecisionWindowResult(
                status="provider_error",
                failure=ProviderFailure(category="transport", message="offline network outage"),
            )
        self.ready.set()
        await self.recovered.wait()
        return await super().decide(request, context)


class _BlockingHarness(_ModelHarness):
    def __init__(self, model_id: str, started: asyncio.Event) -> None:
        super().__init__(model_id)
        self.started = started

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        del request, context
        self.started.set()
        await asyncio.Future()
        raise AssertionError("blocking Harness unexpectedly resumed")


def _spec(*, seeds: tuple[int, ...] = (11,)) -> ExperimentSpec:
    return ExperimentSpec(
        campaign_id="application-test",
        base_url="https://gateway.example/v1",
        company_count=2,
        days=1,
        seeds=seeds,
        api=(
            ApiSpec(api_key="key-one", models=("model-one",)),
            ApiSpec(api_key="key-two", models=("model-two",)),
        ),
        execution={
            "max_concurrent_runs": 3,
            "max_inflight_decisions": 6,
            "max_run_attempts": 2,
        },
    )


@pytest.mark.asyncio
async def test_campaign_runs_every_lineup_across_fixed_multi_key_routes(tmp_path: Path) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec, simulation_fingerprints={"economy": "test"})
    routed: list[tuple[str, int, str]] = []

    def builder(route: RuntimeModelRoute, trace_directory: Path) -> _ModelHarness:
        del trace_directory
        routed.append((route.model_id, route.api_index, route.api_key.get_secret_value()))
        return _ModelHarness(route.model_id)

    application = CampaignApplication(
        spec,
        plan,
        tmp_path / "campaign",
        model_harness_builder=builder,
        lease_seconds=120,
    )
    report = await application.run()

    assert report.is_complete
    assert report.status_counts == {RunJobStatus.COMPLETED.value: 3}
    assert {item[:2] for item in routed} == {("model-one", 0), ("model-two", 1)}
    assert {item[2] for item in routed} == {"key-one", "key-two"}
    assert len(application.repository.list_results(spec.campaign_id)) == 3
    assert len(application.repository.list_replay_receipts(spec.campaign_id)) == 3
    assert application.analysis_path.is_file()
    assert len(tuple((application.output / "runs").glob("**/final_checkpoint.json"))) == 3

    application.analysis_path.unlink()
    database_only = campaign_report(application.output)
    assert database_only.is_complete
    assert database_only.analysis_path is None
    second = await application.run()
    assert second == report
    assert len(routed) == 4


@pytest.mark.asyncio
async def test_budget_exhaustion_is_reported_and_does_not_block_campaign_publication(
    tmp_path: Path,
) -> None:
    spec = _spec()
    application = CampaignApplication(
        spec,
        CampaignCompiler().compile(spec),
        tmp_path,
        model_harness_builder=lambda route, _: _ExhaustedHarness(route.model_id),
    )
    report = await application.run()
    assert report.is_complete
    analysis = json.loads(application.analysis_path.read_bytes())
    for model_id in spec.model_ids:
        assert analysis["health"][model_id]["status_counts"] == {"budget_exhausted": 2}
        assert model_id in analysis["track_a"]
    assert len(application.repository.list_replay_receipts(spec.campaign_id)) == 3


@pytest.mark.asyncio
async def test_campaign_pool_runs_real_offline_workers_and_publishes_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    pools: list[WorkerPool] = []

    def offline_worker(
        route: RuntimeModelRoute, trace_directory: Path, *, worker_pool: WorkerPool | None = None
    ) -> OpenCodeHarness:
        del trace_directory
        assert worker_pool is not None
        pools.append(worker_pool)
        return OpenCodeHarness(
            OpenCodeHarnessConfig(
                base_url="http://127.0.0.1:1/v1",
                model=route.model_id,
                api_key=SecretStr("offline-unused"),
                worker_command=(sys.executable, "-u", str(root / "tests/harness/fake_worker.py")),
                worker_directory=root,
                verify_provenance=False,
            ),
            worker_pool=worker_pool,
        )

    async def offline_catalog(self: ModelRouteRegistry) -> None:
        return None

    monkeypatch.setattr("ceo_arena.campaign.routes._open_code_harness", offline_worker)
    monkeypatch.setattr(ModelRouteRegistry, "validate_catalog", offline_catalog)
    spec = _spec(seeds=(47, 11))
    spec.execution.max_inflight_decisions = 2
    plan = CampaignCompiler().compile(spec)
    application = CampaignApplication(spec, plan, tmp_path, worker_poll_seconds=0.001)

    report = await application.run()

    assert report.is_complete
    assert report.expected_runs == 6
    assert all(pool.resident_count == 0 for pool in pools)
    snapshot = SchedulerSnapshot.model_validate_json(
        (tmp_path / "scheduler_status.json").read_text(encoding="utf-8")
    )
    assert not snapshot.scheduler_running
    assert snapshot.active_runs == snapshot.active_decisions == 0
    assert snapshot.health.total_completed_count == 8
    assert len(application.repository.list_replay_receipts(spec.campaign_id)) == 6


def test_prepare_persists_no_credentials_and_status_needs_no_spec(tmp_path: Path) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    output = tmp_path / "campaign"
    application = CampaignApplication(spec, plan, output)

    report = application.prepare()

    assert report.status_counts == {RunJobStatus.QUEUED.value: 3}
    plan_text = application.plan_path.read_text(encoding="utf-8")
    assert "key-one" not in plan_text
    assert "key-two" not in plan_text
    with sqlite3.connect(application.database_path) as connection:
        dump = "\n".join(connection.iterdump())
    assert "key-one" not in dump
    assert "key-two" not in dump
    assert campaign_report(output) == report


def test_prepare_repairs_one_missing_projection_without_poisoning_the_other(
    tmp_path: Path,
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    output = tmp_path / "campaign"
    original = CampaignApplication(spec, plan, output)
    original.prepare()
    wrong_spec = spec.model_copy(update={"days": 2})
    wrong_plan = CampaignCompiler().compile(wrong_spec)

    original.plan_path.unlink()
    with pytest.raises(RepositoryConflictError):
        CampaignApplication(wrong_spec, wrong_plan, output).prepare()
    assert not original.plan_path.exists()
    CampaignApplication(spec, plan, output).prepare()
    assert original.plan_path.is_file()

    for suffix in ("", "-wal", "-shm"):
        Path(f"{original.database_path}{suffix}").unlink(missing_ok=True)
    with pytest.raises(RepositoryConflictError):
        CampaignApplication(wrong_spec, wrong_plan, output).prepare()
    repaired = CampaignApplication(spec, plan, output)
    repaired.prepare()
    assert repaired.repository.get_campaign(spec.campaign_id) is not None


def test_compiled_lineup_with_two_models_uses_both_api_entries(tmp_path: Path) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    routes_seen: list[tuple[str, int]] = []

    def builder(route: RuntimeModelRoute, trace_directory: Path) -> _ModelHarness:
        del trace_directory
        routes_seen.append((route.model_id, route.api_index))
        return _ModelHarness(route.model_id)

    application = CampaignApplication(
        spec,
        plan,
        tmp_path,
        model_harness_builder=builder,
    )
    provider_run = next(run for run in plan.runs if RULE_MODEL_ID not in run.model_ids)

    harnesses = application.routes.create_harnesses(
        provider_run,
        tmp_path / "traces",
    )

    assert set(harnesses) == {"company-1", "company-2"}
    assert routes_seen == [("model-one", 0), ("model-two", 1)]
    assert json.loads(provider_run.model_dump_json())["api_indices"] == [0, 1]


@pytest.mark.asyncio
async def test_provider_recovery_keeps_healthy_decisions_and_the_same_run_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    ready, recovered = asyncio.Event(), asyncio.Event()
    failures: list[_RecoveringHarness] = []

    def builder(route: RuntimeModelRoute, _directory: Path) -> _ModelHarness:
        if route.model_id == "model-two":
            return _ModelHarness(route.model_id)
        harness = _RecoveringHarness(route.model_id, ready, recovered)
        failures.append(harness)
        return harness

    monkeypatch.setattr(ProviderRetryPolicy, "delay", lambda *_args: 0)
    application = CampaignApplication(
        spec,
        plan,
        tmp_path,
        model_harness_builder=builder,
        worker_poll_seconds=0.001,
    )
    run = next(item for item in plan.runs if RULE_MODEL_ID not in item.model_ids)
    execution = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        await _wait_for_staged(application, run.task_key, "company-2")
        frontier = application.repository.load_recovery(run.task_key)
        assert frontier is not None and frontier.checkpoint is not None
        assert frontier.job.status is RunJobStatus.RUNNING
        assert frontier.job.attempt == 1
        assert frontier.checkpoint.current_state.day == 0
        assert application.repository.list_failure_history(run.task_key) == ()
        assert application.repository.get_analysis(spec.campaign_id) is None
        assert not application.analysis_path.exists()
        recovered.set()
        report = await asyncio.wait_for(execution, timeout=10)
        assert report.is_complete
        assert all(harness.calls == 5 for harness in failures)
        assert all(job.attempt == 1 for job in application.repository.list_jobs(spec.campaign_id))
        assert len(application.repository.list_replay_receipts(spec.campaign_id)) == 3
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def _wait_for_staged(
    application: CampaignApplication,
    task_key: str,
    company_id: str,
) -> None:
    """Bound polling to the receipt's actual durable publication, not Harness timing."""
    async with asyncio.timeout(5):
        while True:
            frontier = application.repository.load_recovery(task_key)
            if frontier and any(item.company_id == company_id for item in frontier.decisions):
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_provider_backoff_is_user_pausable_without_publishing_or_exhausting_run(
    tmp_path: Path,
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    failed = asyncio.Event()
    application = CampaignApplication(
        spec,
        plan,
        tmp_path,
        model_harness_builder=lambda route, _directory: (
            _FailingHarness(
                route.model_id,
                failed,
                ProviderFailure(category="transport", message="offline network outage"),
            )
            if route.model_id == "model-one"
            else _ModelHarness(route.model_id)
        ),
        heartbeat_seconds=0.01,
    )
    application.prepare()
    run = next(item for item in plan.runs if RULE_MODEL_ID not in item.model_ids)
    claimed = application.repository.claim_task(
        run.task_key,
        lease_owner="provider-pause",
        lease_seconds=120,
    )
    assert claimed is not None
    execution = asyncio.create_task(
        application._run_claimed(
            claimed,
            "provider-pause",
            BoundedDecisionAdmission(6),
        )
    )
    try:
        await asyncio.wait_for(failed.wait(), timeout=5)
        await _wait_for_staged(application, run.task_key, "company-2")
        current = application.repository.get_job(run.task_key)
        assert current is not None
        application.repository.request_pause(run.task_key, expected_revision=current.revision)
        await asyncio.wait_for(execution, timeout=5)
        stopped = application.repository.get_job(run.task_key)
        assert stopped is not None and stopped.status is RunJobStatus.STOPPED
        assert stopped.stop_reason is RunStopReason.USER_REQUESTED
        assert stopped.attempt == 1
        assert application.repository.list_failure_history(run.task_key) == ()
        assert application.repository.list_results(spec.campaign_id) == ()
        assert not application.analysis_path.exists()
        frontier = application.repository.load_recovery(run.task_key)
        assert frontier is not None
        assert [item.company_id for item in frontier.decisions] == ["company-2"]
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_explicit_resume_requeues_stopped_runs(tmp_path: Path) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    application = CampaignApplication(
        spec,
        plan,
        tmp_path,
        model_harness_builder=lambda route, _directory: _ModelHarness(route.model_id),
    )
    application.prepare()
    run = plan.runs[0]
    claimed = application.repository.claim_task(
        run.task_key,
        lease_owner="stop-test",
        lease_seconds=120,
    )
    assert claimed is not None
    application.repository.stop_run(
        run.task_key,
        lease_owner="stop-test",
        expected_revision=claimed.revision,
        reason=RunStopReason.USER_REQUESTED,
        error="explicit test stop",
    )

    report = await application.resume()

    assert report.is_complete
    assert report.status_counts == {RunJobStatus.COMPLETED.value: 3}


@pytest.mark.asyncio
async def test_heartbeat_pauses_an_active_decision_without_failure_history(
    tmp_path: Path,
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    started = asyncio.Event()
    application = CampaignApplication(
        spec,
        plan,
        tmp_path,
        model_harness_builder=lambda route, _directory: _BlockingHarness(
            route.model_id,
            started,
        ),
        heartbeat_seconds=0.01,
        retry_backoff=lambda _task_key, _attempt: 0,
    )
    application.prepare()
    run = next(item for item in plan.runs if RULE_MODEL_ID not in item.model_ids)
    claimed = application.repository.claim_task(
        run.task_key,
        lease_owner="pause-worker",
        lease_seconds=120,
    )
    assert claimed is not None
    execution = asyncio.create_task(
        application._run_claimed(
            claimed,
            "pause-worker",
            BoundedDecisionAdmission(plan.execution.max_inflight_decisions),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    current = application.repository.get_job(run.task_key)
    assert current is not None

    paused = application.repository.request_pause(
        run.task_key,
        expected_revision=current.revision,
    )
    await asyncio.wait_for(execution, timeout=2)
    stopped = application.repository.get_job(run.task_key)

    assert paused.revision == current.revision
    assert stopped is not None
    assert stopped.status is RunJobStatus.STOPPED
    assert stopped.stop_reason is RunStopReason.USER_REQUESTED
    assert application.repository.list_failure_history(run.task_key) == ()


def test_retry_backoff_is_deterministic_exponential_and_capped() -> None:
    first = _deterministic_retry_backoff("task", 1)

    assert first == _deterministic_retry_backoff("task", 1)
    assert 5 <= first <= 10
    assert first < _deterministic_retry_backoff("task", 4)
    assert _deterministic_retry_backoff("task", 100) <= 300


@pytest.mark.asyncio
async def test_scheduler_waits_for_a_retryable_job_instead_of_exiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    plan = CampaignCompiler().compile(spec)
    application = CampaignApplication(
        spec,
        plan,
        tmp_path,
        model_harness_builder=lambda route, _directory: _ModelHarness(route.model_id),
        worker_poll_seconds=0.001,
    )
    application.prepare()
    jobs = application.repository.list_jobs(spec.campaign_id)
    for job in jobs[:-1]:
        application.repository.request_pause(job.task_key, expected_revision=job.revision)

    claim_next = application.repository.claim_next
    calls = 0

    def delayed_claim(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return claim_next(*args, **kwargs)

    monkeypatch.setattr(application.repository, "claim_next", delayed_claim)
    await application.run()

    completed = application.repository.get_job(jobs[-1].task_key)
    assert calls >= 2
    assert completed is not None and completed.status is RunJobStatus.COMPLETED
