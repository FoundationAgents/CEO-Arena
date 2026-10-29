from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.records import (
    RunStopReason,
)
from ceo_arena.campaign.routes import RuntimeModelRoute
from ceo_arena.campaign.spec import load_experiment_spec
from ceo_arena.dashboard.control import (
    CampaignControl,
    CampaignControlError,
    CampaignControlErrorCode,
    ExperimentAction,
    ExperimentStatus,
    StartDisposition,
)
from ceo_arena.dashboard.process import CampaignProcessRequest, DetachedProcessAdapter
from ceo_arena.harness import OPENCODE_LLM_COMMIT
from ceo_arena.models import CompanyDecision, DecisionWindowRequest, DecisionWindowResult
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime import CompanyContext
from ceo_arena.runtime.lock import RunDirectoryLock


@dataclass(slots=True)
class _RecordingHandle:
    running: bool = True
    code: int | None = None

    def is_running(self) -> bool:
        return self.running

    def exit_code(self) -> int | None:
        return None if self.running else self.code


class _RecordingProcessAdapter:
    def __init__(self) -> None:
        self.requests: list[CampaignProcessRequest] = []
        self.handles: list[_RecordingHandle] = []

    def start(self, request: CampaignProcessRequest) -> _RecordingHandle:
        self.requests.append(request)
        handle = _RecordingHandle()
        self.handles.append(handle)
        return handle


class _SealingHarness:
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

    async def aclose(self) -> None:
        return None


def _write_config(
    path: Path,
    *,
    campaign_id: str = "ui-campaign",
    models: tuple[str, ...] = ("model-one", "model-two"),
    company_count: int = 2,
    days: int = 7,
    seeds: tuple[int, ...] = (11,),
    api_key: str = "PRIVATE_TEST_KEY",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    model_values = ", ".join(json.dumps(model) for model in models)
    seed_values = ", ".join(str(seed) for seed in seeds)
    path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                f'campaign_id = "{campaign_id}"',
                'base_url = "https://gateway.example/v1"',
                f"company_count = {company_count}",
                f"days = {days}",
                f"seeds = [{seed_values}]",
                "",
                "[[api]]",
                f'api_key = "{api_key}"',
                f"models = [{model_values}]",
                "",
                "[execution]",
                "max_concurrent_runs = 5",
                "max_inflight_decisions = 16",
                "max_run_attempts = 3",
                'schedule = "seed_wave"',
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _prepared_application(config: Path, output: Path) -> CampaignApplication:
    spec = load_experiment_spec(config)
    plan = CampaignCompiler().compile(
        spec,
        simulation_fingerprints=simulation_fingerprints(),
    )
    application = CampaignApplication(spec, plan, output)
    application.prepare()
    return application


def test_start_is_idempotent_and_never_exposes_private_inputs(tmp_path: Path) -> None:
    config_root = tmp_path / "private-configs"
    runs_root = tmp_path / "runs"
    config = config_root / "campaign.toml"
    _write_config(config)
    process = _RecordingProcessAdapter()
    control = CampaignControl(config_root, runs_root, process=process)
    experiment = control.snapshot().experiments[0]

    first = control.start(experiment.experiment_ref, "request-00000001")
    duplicate = control.start(experiment.experiment_ref, "request-00000001")
    already_running = control.start(experiment.experiment_ref, "request-00000002")

    assert first == duplicate
    assert first.disposition is StartDisposition.STARTED
    assert already_running.disposition is StartDisposition.ALREADY_RUNNING
    assert len(process.requests) == 1
    assert process.requests[0].mode == "run"
    live = control.snapshot().experiments[0]
    assert live.status is ExperimentStatus.STARTING
    serialized = control.snapshot().model_dump_json()
    assert "PRIVATE_TEST_KEY" not in serialized
    assert str(config_root) not in serialized
    assert str(runs_root) not in serialized

    restarted_process = _RecordingProcessAdapter()
    restarted = CampaignControl(config_root, runs_root, process=restarted_process)
    persisted = restarted.start(experiment.experiment_ref, "request-00000001")
    assert persisted == first
    assert restarted_process.requests == []
    receipt_text = next((runs_root / ".control-receipts").glob("*.json")).read_text(
        encoding="utf-8"
    )
    assert "PRIVATE_TEST_KEY" not in receipt_text
    assert str(config_root) not in receipt_text


def test_revision_probe_does_not_parse_or_compile_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "configs"
    _write_config(config_root / "campaign.toml")
    control = CampaignControl(config_root, tmp_path / "runs", process=_RecordingProcessAdapter())
    monkeypatch.setattr(
        "ceo_arena.dashboard.control.CampaignCompiler.compile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compile called")),
    )

    revision = control.revision()

    assert len(revision) == 64


def test_snapshot_includes_complete_long_lineup_and_detects_plan_conflict(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    config = config_root / "arena.toml"
    models = ("model-a", "model-b", "model-c", "model-d")
    _write_config(
        config,
        campaign_id="long-arena",
        models=models,
        company_count=4,
        days=500,
        seeds=(11, 29),
    )
    process = _RecordingProcessAdapter()
    control = CampaignControl(config_root, runs_root, process=process)
    original = control.snapshot().experiments[0]

    assert original.model_ids == (*models, "deterministic-rule-ceo")
    assert original.expected_runs == 10
    assert original.completed_runs == 0
    assert original.total_days == 500
    assert original.seed_count == 2
    control.start(original.experiment_ref, "request-long-001")

    _write_config(
        config,
        campaign_id="long-arena",
        models=models,
        company_count=4,
        days=499,
        seeds=(11, 29),
    )
    conflicted = control.snapshot().experiments[0]
    assert conflicted.status is ExperimentStatus.CONFLICT
    assert not conflicted.can_start
    with pytest.raises(CampaignControlError) as captured:
        control.start(conflicted.experiment_ref, "request-long-002")
    assert captured.value.code is CampaignControlErrorCode.CONFIG_CONFLICT
    assert len(process.requests) == 1


def test_worker_exit_projects_safe_retryable_failure(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    _write_config(config_root / "campaign.toml")
    process = _RecordingProcessAdapter()
    control = CampaignControl(config_root, runs_root, process=process)
    experiment = control.snapshot().experiments[0]
    control.start(experiment.experiment_ref, "request-worker-exit")
    process.handles[0].running = False
    process.handles[0].code = 2

    failed = control.snapshot().experiments[0]

    assert failed.status is ExperimentStatus.INTERRUPTED
    assert failed.can_start
    assert failed.action is ExperimentAction.RUN
    assert "Runner" in failed.message
    serialized = failed.model_dump_json()
    assert "PRIVATE_TEST_KEY" not in serialized
    assert str(config_root) not in serialized

    restarted = (
        CampaignControl(
            config_root,
            runs_root,
            process=_RecordingProcessAdapter(),
        )
        .snapshot()
        .experiments[0]
    )
    assert restarted.status is ExperimentStatus.INTERRUPTED
    assert restarted.can_start
    assert "preflight" in restarted.message


def test_invalid_config_error_is_redacted_and_path_opaque(tmp_path: Path) -> None:
    config_root = tmp_path / "secret-folder"
    config_root.mkdir()
    secret = "sk-DO-NOT-EXPOSE-123"
    config = config_root / "private-name.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'campaign_id = "broken"',
                'base_url = "https://gateway.example/v1"',
                'company_count = "not-an-integer"',
                "days = 7",
                "seeds = [11]",
                "[[api]]",
                f'api_key = "{secret}"',
                'models = ["model-one"]',
            )
        ),
        encoding="utf-8",
    )
    control = CampaignControl(config_root, tmp_path / "runs", process=_RecordingProcessAdapter())

    catalog = control.snapshot()
    assert len(catalog.experiments) == 1
    invalid = catalog.experiments[0]
    assert invalid.status is ExperimentStatus.INVALID
    assert not invalid.can_start
    serialized = catalog.model_dump_json()
    assert secret not in serialized
    assert config.name not in serialized
    assert str(config_root) not in serialized
    with pytest.raises(CampaignControlError) as captured:
        control.start(invalid.experiment_ref, "request-invalid")
    assert captured.value.code is CampaignControlErrorCode.INVALID_CONFIG
    assert secret not in str(captured.value)


def test_existing_unmanaged_output_directory_is_never_reused(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    _write_config(config_root / "campaign.toml", campaign_id="occupied")
    occupied = runs_root / "occupied"
    occupied.mkdir(parents=True)
    (occupied / "manifest.json").write_text("{}", encoding="utf-8")
    control = CampaignControl(config_root, runs_root, process=_RecordingProcessAdapter())

    experiment = control.snapshot().experiments[0]

    assert experiment.status is ExperimentStatus.CONFLICT
    assert not experiment.can_start
    assert not (occupied / "campaign.sqlite3").exists()


def test_large_opted_in_campaign_is_cli_only_and_never_prepared(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    config = config_root / "large.toml"
    _write_config(
        config,
        campaign_id="large-campaign",
        models=tuple(f"model-{index}" for index in range(9)),
        company_count=9,
        seeds=tuple(range(51)),
    )
    with config.open("a", encoding="utf-8") as stream:
        stream.write("allow_large_campaign = true\n")
    control = CampaignControl(config_root, runs_root, process=_RecordingProcessAdapter())
    experiment = control.snapshot().experiments[0]

    assert experiment.expected_runs > 500
    assert experiment.status is ExperimentStatus.INVALID
    assert not experiment.can_start
    with pytest.raises(CampaignControlError) as captured:
        control.start(experiment.experiment_ref, "request-large-campaign")
    assert captured.value.code is CampaignControlErrorCode.NOT_STARTABLE
    assert not (runs_root / "large-campaign" / "campaign.sqlite3").exists()


def test_existing_lock_is_authoritative_and_stopped_jobs_choose_resume(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    config = config_root / "campaign.toml"
    _write_config(config)
    output = runs_root / "ui-campaign"
    application = _prepared_application(config, output)
    process = _RecordingProcessAdapter()
    control = CampaignControl(config_root, runs_root, process=process)
    experiment_ref = control.snapshot().experiments[0].experiment_ref

    with RunDirectoryLock(output / ".campaign.lock"):
        active = control.snapshot().experiments[0]
        assert active.status is ExperimentStatus.RUNNING
        receipt = control.start(experiment_ref, "request-active")
        assert receipt.disposition is StartDisposition.ALREADY_RUNNING
    assert process.requests == []

    job = application.repository.list_jobs("ui-campaign")[0]
    claimed = application.repository.claim_task(
        job.task_key,
        lease_owner="test-owner",
        lease_seconds=120,
    )
    assert claimed is not None
    application.repository.stop_run(
        job.task_key,
        lease_owner="test-owner",
        expected_revision=claimed.revision,
        reason=RunStopReason.USER_REQUESTED,
        error="explicit test stop",
    )
    stopped = control.snapshot().experiments[0]
    assert stopped.action is ExperimentAction.RESUME
    control.start(experiment_ref, "request-resume")
    assert process.requests[-1].mode == "resume"


def test_existing_completed_campaign_is_not_spawned_again(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    config = config_root / "complete.toml"
    _write_config(
        config,
        campaign_id="complete-campaign",
        models=("model-one",),
        company_count=1,
        days=1,
    )
    spec = load_experiment_spec(config)
    plan = CampaignCompiler().compile(
        spec,
        simulation_fingerprints=simulation_fingerprints(),
    )

    def builder(route: RuntimeModelRoute, trace_directory: Path) -> _SealingHarness:
        del trace_directory
        return _SealingHarness(route.model_id)

    application = CampaignApplication(
        spec,
        plan,
        runs_root / spec.campaign_id,
        model_harness_builder=builder,
    )
    assert asyncio.run(application.run()).is_complete
    process = _RecordingProcessAdapter()
    control = CampaignControl(config_root, runs_root, process=process)
    completed = control.snapshot().experiments[0]

    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.completed_runs == completed.expected_runs == 2
    receipt = control.start(completed.experiment_ref, "request-completed")
    assert receipt.disposition is StartDisposition.ALREADY_COMPLETED
    assert process.requests == []

    with sqlite3.connect(application.database_path) as connection:
        connection.execute(
            "UPDATE campaigns SET status = 'incomplete' WHERE campaign_id = ?",
            (spec.campaign_id,),
        )
        connection.execute(
            "DELETE FROM campaign_analyses WHERE campaign_id = ?",
            (spec.campaign_id,),
        )
    finalizing = control.snapshot().experiments[0]
    assert finalizing.status is ExperimentStatus.INTERRUPTED
    assert finalizing.action is ExperimentAction.RUN
    assert finalizing.can_start
    control.start(finalizing.experiment_ref, "request-finalize")
    assert process.requests[-1].mode == "run"


def test_detached_process_adapter_uses_argument_array_and_private_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Process:
        def poll(self) -> None:
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr("ceo_arena.dashboard.process.subprocess.Popen", fake_popen)
    output = tmp_path / "runs" / "campaign"
    output.mkdir(parents=True)
    request = CampaignProcessRequest(
        campaign_id="campaign",
        mode="run",
        spec_path=tmp_path / "private.toml",
        output=output,
    )

    handle = DetachedProcessAdapter(tmp_path / "python.exe").start(request)

    command = tuple(captured["command"])
    assert command[:5] == (
        str((tmp_path / "python.exe").resolve()),
        "-m",
        "ceo_arena.cli",
        "campaign",
        "run",
    )
    assert captured["shell"] is False
    assert captured["close_fds"] is True
    assert handle.is_running()
    assert "PRIVATE_TEST_KEY" not in " ".join(command)
    logs = tuple((output / ".control-logs").glob("*.log"))
    assert len(logs) == 2
