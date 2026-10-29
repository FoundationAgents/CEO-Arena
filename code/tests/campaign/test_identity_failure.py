"""Keep routing-integrity failures out of economic scores and automatic retries."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.records import FailurePhase, RunJobStatus
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.harness import OPENCODE_LLM_COMMIT
from ceo_arena.models import (
    AgentUsage,
    CompanyDecision,
    DecisionWindowRequest,
    DecisionWindowResult,
    ProviderFailure,
)
from ceo_arena.runtime import CompanyContext
from ceo_arena.runtime.persistence import CheckpointPhase


class _ProviderHarness:
    harness_id = f"opencode-native-{OPENCODE_LLM_COMMIT[:12]}"

    def __init__(
        self,
        model_id: str,
        calls: Counter[tuple[str, str]],
        failure: ProviderFailure | None,
    ) -> None:
        self.model_id = model_id
        self.calls = calls
        self.failure = failure

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        del context
        self.calls[request.run_id, request.company_id] += 1
        if self.failure:
            return DecisionWindowResult(status="provider_error", failure=self.failure)
        return DecisionWindowResult(
            status="sealed",
            decision=CompanyDecision(
                company_id=request.company_id, world_version=request.world_version
            ),
            usage=AgentUsage(provider_turns=1),
        )

    async def cancel(self, company_id: str) -> None:
        del company_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_name"),
    [
        (
            ProviderFailure(category="identity", message="model mismatch", retryable=False),
            "ProviderIdentityError",
        ),
        (
            ProviderFailure(category="http", http_status=422, message="invalid conversation"),
            "ProviderPermanentError",
        ),
        (
            ProviderFailure(category="auth", http_status=401, message="invalid key"),
            "ProviderPermanentError",
        ),
    ],
)
async def test_permanent_provider_failure_is_terminal_without_retrying_or_settling_day_zero(
    tmp_path: Path,
    failure: ProviderFailure,
    error_name: str,
) -> None:
    spec = ExperimentSpec(
        campaign_id="identity-integrity",
        base_url="https://offline.invalid/v1",
        company_count=2,
        days=1,
        seeds=(11,),
        api=(ApiSpec(api_key="offline-only", models=("model-one", "model-two")),),
        execution={"max_concurrent_runs": 3, "max_run_attempts": None},
    )
    calls: Counter[tuple[str, str]] = Counter()
    application = CampaignApplication(
        spec,
        CampaignCompiler().compile(spec),
        tmp_path,
        model_harness_builder=lambda route, _: _ProviderHarness(route.model_id, calls, failure),
        worker_poll_seconds=0.01,
    )
    report = await asyncio.wait_for(application.run(), timeout=10)

    assert report.status_counts == {RunJobStatus.FAILED.value: 3}
    assert not report.is_complete
    assert calls and set(calls.values()) == {1}
    for job in application.repository.list_jobs(spec.campaign_id):
        assert job.attempt == 1 and job.current_day == 0
        assert job.retry_not_before is None
        failures = application.repository.list_failure_history(job.task_key)
        assert len(failures) == 1 and failures[0].phase is FailurePhase.PROVIDER
        assert error_name in failures[0].error
        frontier = application.repository.load_recovery(job.task_key)
        assert frontier is not None and frontier.checkpoint is not None
        checkpoint = frontier.checkpoint
        assert checkpoint.phase is CheckpointPhase.COLLECTING_DECISIONS
        assert checkpoint.current_state == checkpoint.initial_state
        assert checkpoint.current_state.day == 0
        assert checkpoint.completed_windows == () and checkpoint.pending_window is None

    assert application.repository.list_results(spec.campaign_id) == ()
    assert application.repository.list_replay_receipts(spec.campaign_id) == ()
    assert not application.analysis_path.exists()
    previous_calls = calls.copy()
    repeated = await asyncio.wait_for(application.run(), timeout=5)
    assert repeated.status_counts == report.status_counts
    assert calls == previous_calls


@pytest.mark.asyncio
async def test_bad_provider_stops_only_affected_runs(tmp_path: Path) -> None:
    spec = ExperimentSpec(
        campaign_id="isolated-permanent-error",
        base_url="https://offline.invalid/v1",
        company_count=2,
        days=1,
        seeds=(11,),
        api=(ApiSpec(api_key="offline-only", models=("broken-model", "healthy-model")),),
        execution={"max_concurrent_runs": 3, "max_run_attempts": None},
    )
    calls: Counter[tuple[str, str]] = Counter()
    failure = ProviderFailure(category="http", http_status=403, message="access denied")
    application = CampaignApplication(
        spec,
        CampaignCompiler().compile(spec),
        tmp_path,
        model_harness_builder=lambda route, _: _ProviderHarness(
            route.model_id, calls, failure if route.model_id == "broken-model" else None
        ),
        worker_poll_seconds=0.01,
    )
    report = await asyncio.wait_for(application.run(), 10)
    assert report.status_counts == {RunJobStatus.FAILED.value: 2, RunJobStatus.COMPLETED.value: 1}
    assert all(count == 1 for count in calls.values())
    completed = application.repository.list_results(spec.campaign_id)
    assert len(completed) == 1
    assert all(item.model_id != "broken-model" for item in completed[0].assignments)
