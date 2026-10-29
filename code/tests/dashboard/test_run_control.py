from __future__ import annotations

from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.records import (
    FailurePhase,
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.dashboard.catalog import RunDashboard
from ceo_arena.dashboard.control import CampaignControl
from ceo_arena.dashboard.models import RunControlRequest
from ceo_arena.dashboard.process import CampaignProcessRequest
from ceo_arena.dashboard.run_control import RunControl, RunControlError, RunControlErrorCode
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import lineup_run_slug
from ceo_arena.runtime import RunManifest, RunStore
from ceo_arena.runtime.persistence import CheckpointPhase, RunCheckpoint

from .test_campaign_dashboard import _config
from .test_control import (
    _prepared_application,
    _RecordingHandle,
    _RecordingProcessAdapter,
    _write_config,
)


class _FlakyProcessAdapter:
    def __init__(self) -> None:
        self.requests: list[CampaignProcessRequest] = []

    def start(self, request: CampaignProcessRequest) -> _RecordingHandle:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise RuntimeError("synthetic launch failure")
        return _RecordingHandle()


def _control_fixture(
    tmp_path: Path,
    *,
    process: _RecordingProcessAdapter | _FlakyProcessAdapter,
) -> tuple[RunControl, RunDashboard, CampaignApplication]:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    config = config_root / "campaign.toml"
    _write_config(config)
    application = _prepared_application(config, runs_root / "ui-campaign")
    dashboard = RunDashboard(runs_root)
    campaigns = CampaignControl(config_root, runs_root, process=process)
    return RunControl(dashboard, campaigns), dashboard, application


def _interrupt_first_run(application: CampaignApplication) -> None:
    job = application.repository.list_jobs("ui-campaign")[0]
    claimed = application.repository.claim_task(
        job.task_key,
        lease_owner="control-test-worker",
        lease_seconds=120,
    )
    assert claimed is not None
    application.repository.interrupt_run(
        claimed.task_key,
        lease_owner="control-test-worker",
        expected_revision=claimed.revision,
        error="synthetic recoverable interruption",
        phase=FailurePhase.RUNTIME,
    )


def test_run_control_pauses_and_resumes_one_revision_idempotently(tmp_path: Path) -> None:
    process = _RecordingProcessAdapter()
    control, dashboard, application = _control_fixture(tmp_path, process=process)
    _interrupt_first_run(application)
    selected = dashboard.list_runs().runs[0]
    assert selected.control_action == "pause"

    paused = control.control(
        selected.run_ref,
        RunControlRequest(action="pause", revision=selected.revision),
        "pause-request-001",
    )
    stopped = dashboard.get_run(selected.run_ref)

    assert paused.status == RunJobStatus.STOPPED.value
    assert paused.control_action == "resume"
    assert stopped.control_action == "resume"
    assert stopped.control_label == "Resume"
    assert stopped.revision == paused.revision

    request = RunControlRequest(action="resume", revision=stopped.revision)
    resumed = control.control(selected.run_ref, request, "resume-request-001")
    duplicate = control.control(selected.run_ref, request, "resume-request-001")
    job = application.repository.get_job(
        application.repository.list_jobs("ui-campaign")[0].task_key
    )

    assert resumed == duplicate
    assert resumed.status == RunJobStatus.QUEUED.value
    assert resumed.revision == stopped.revision + 1
    assert job is not None
    assert job.attempt_limit is None
    assert len(process.requests) == 1

    with pytest.raises(RunControlError) as stale:
        control.control(selected.run_ref, request, "resume-request-stale")
    assert stale.value.code is RunControlErrorCode.REVISION_CONFLICT


def test_resume_launch_failure_is_retryable_without_a_second_cas(tmp_path: Path) -> None:
    process = _FlakyProcessAdapter()
    control, dashboard, application = _control_fixture(tmp_path, process=process)
    _interrupt_first_run(application)
    selected = dashboard.list_runs().runs[0]
    control.control(
        selected.run_ref,
        RunControlRequest(action="pause", revision=selected.revision),
        "pause-before-retry",
    )
    stopped = dashboard.get_run(selected.run_ref)
    request = RunControlRequest(action="resume", revision=stopped.revision)

    with pytest.raises(RunControlError) as failed:
        control.control(selected.run_ref, request, "resume-with-retry")
    assert failed.value.code is RunControlErrorCode.LAUNCH_FAILED
    assert failed.value.retryable
    queued = application.repository.list_jobs("ui-campaign")[0]
    assert queued.status is RunJobStatus.QUEUED

    restarted_dashboard = RunDashboard(tmp_path / "runs")
    restarted_campaigns = CampaignControl(
        tmp_path / "configs",
        tmp_path / "runs",
        process=process,
    )
    restarted_control = RunControl(restarted_dashboard, restarted_campaigns)
    retry_view = restarted_dashboard.get_run(selected.run_ref)
    assert retry_view.control_action == "resume"
    receipt = restarted_control.control(
        selected.run_ref,
        RunControlRequest(action="resume", revision=retry_view.revision),
        "resume-after-dashboard-restart",
    )
    assert receipt.status == RunJobStatus.QUEUED.value
    assert application.repository.list_jobs("ui-campaign")[0].revision == queued.revision
    assert len(process.requests) == 2


def test_pause_does_not_require_the_campaign_toml(tmp_path: Path) -> None:
    process = _RecordingProcessAdapter()
    control, dashboard, application = _control_fixture(tmp_path, process=process)
    _interrupt_first_run(application)
    selected = dashboard.list_runs().runs[0]
    (tmp_path / "configs" / "campaign.toml").unlink()

    receipt = control.control(
        selected.run_ref,
        RunControlRequest(action="pause", revision=selected.revision),
        "pause-without-config",
    )

    assert receipt.status == RunJobStatus.STOPPED.value
    assert process.requests == []


def test_projection_distinguishes_provider_and_permanent_failures(tmp_path: Path) -> None:
    process = _RecordingProcessAdapter()
    _control, dashboard, application = _control_fixture(tmp_path, process=process)
    first, second, *_rest = application.repository.list_jobs("ui-campaign")

    provider = application.repository.claim_task(
        first.task_key,
        lease_owner="provider-worker",
        lease_seconds=120,
    )
    assert provider is not None
    config = _config(application, provider.run_id)
    world = MarketWorld(config.scenario)
    initial_state = world.create_initial_state(config.company_ids)
    run = provider.run_payload
    run_directory = (
        application.output / "runs" / f"seed-{run.seed}" / lineup_run_slug(run.lineup_id)
    )
    RunStore(run_directory).ensure_manifest(
        RunManifest.create(config, world.state_hash(initial_state))
    )
    provider = application.repository.save_checkpoint(
        provider.task_key,
        lease_owner="provider-worker",
        expected_revision=provider.revision,
        checkpoint=RunCheckpoint(
            run_id=config.run_id,
            config_hash=config.config_hash,
            phase=CheckpointPhase.READY,
            initial_state=initial_state,
            current_state=initial_state,
            next_window_index=0,
        ),
    )
    application.repository.stop_run(
        provider.task_key,
        lease_owner="provider-worker",
        expected_revision=provider.revision,
        reason=RunStopReason.PROVIDER_RETRY_EXHAUSTED,
        error="provider attempts exhausted",
        phase=FailurePhase.PROVIDER,
    )

    permanent = application.repository.claim_task(
        second.task_key,
        lease_owner="permanent-worker",
        lease_seconds=120,
    )
    assert permanent is not None
    application.repository.fail_run(
        permanent.task_key,
        lease_owner="permanent-worker",
        expected_revision=permanent.revision,
        error="permanent configuration failure",
        phase=FailurePhase.CONFIGURATION,
    )

    by_id = {run.run_id: run for run in dashboard.list_runs().runs}
    assert by_id[first.run_id].control_action == "resume"
    assert by_id[first.run_id].control_label == "Resume"
    assert by_id[second.run_id].control_action == "none"
    assert by_id[second.run_id].control_label == "Not recoverable"
