from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Literal

import pytest

import ceo_arena.dashboard.campaign as campaign_reader
from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.resources import DecisionHealth, ResourceSample, SchedulerSnapshot
from ceo_arena.campaign.routes import RuntimeModelRoute
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.dashboard import RunDashboard
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import lineup_run_slug
from ceo_arena.harness import OPENCODE_LLM_COMMIT
from ceo_arena.models import CompanyDecision, DecisionWindowRequest, DecisionWindowResult
from ceo_arena.runtime import (
    AgentAssignment,
    CompanyContext,
    RunConfig,
    RunManifest,
    RunStore,
)
from ceo_arena.runtime.persistence import CheckpointPhase, RunCheckpoint, SealedDecision


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


def _spec(
    campaign_id: str,
    *,
    days: int = 2,
    seeds: tuple[int, ...] = (17,),
    schedule: Literal["seed_wave", "seed_priority"] = "seed_wave",
) -> ExperimentSpec:
    return ExperimentSpec(
        campaign_id=campaign_id,
        base_url="https://gateway.example/v1",
        company_count=1,
        days=days,
        seeds=seeds,
        api=(ApiSpec(api_key="PRIVATE_TEST_KEY", models=("model-one",)),),
        execution={
            "max_concurrent_runs": 2,
            "max_inflight_decisions": 2,
            "max_run_attempts": 2,
            "schedule": schedule,
        },
    )


def _application(spec: ExperimentSpec, output: Path) -> CampaignApplication:
    plan = CampaignCompiler().compile(spec, simulation_fingerprints={"economy": "test"})

    def builder(route: RuntimeModelRoute, trace_directory: Path) -> _SealingHarness:
        del trace_directory
        return _SealingHarness(route.model_id)

    return CampaignApplication(spec, plan, output, model_harness_builder=builder)


def _config(application: CampaignApplication, run_id: str) -> RunConfig:
    run = next(item for item in application.plan.runs if item.run_id == run_id)
    assignments = tuple(
        AgentAssignment(
            company_id=company_id,
            model_id=model_id,
            harness_id=application.routes.harness_id(model_id),
            api_index=api_index,
        )
        for company_id, model_id, api_index in zip(
            run.company_ids,
            run.model_ids,
            run.api_indices,
            strict=True,
        )
    )
    return RunConfig(
        run_id=run.run_id,
        scenario=application.plan.scenario_template.model_copy(update={"seed": run.seed}),
        assignments=assignments,
        budget=application.plan.budget,
        provider_endpoint_sha256=application.plan.provider_endpoint_sha256,
        artifact_fingerprints=application.plan.simulation_fingerprints,
    )


def test_campaign_listing_never_parses_world_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(_spec("light-list", days=1), tmp_path / "campaign")
    asyncio.run(application.run())
    dashboard = RunDashboard(tmp_path)

    def reject_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("listing must not deserialize a checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(RunCheckpoint, "model_validate_json", reject_parse)
        listing = dashboard.list_runs()
        assert len(listing.runs) == 2
        assert all(item.status == "complete" for item in listing.runs)
    detail = dashboard.get_run(listing.runs[0].run_ref)
    assert detail.detail_available and detail.companies


def test_dashboard_discovers_late_nested_campaign_and_staged_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    dashboard = RunDashboard(runs_root)
    assert dashboard.list_runs().runs == ()
    initial_revision = dashboard.revision()

    output = runs_root / "campaign-live"
    application = _application(_spec("campaign-live", days=7), output)
    application.prepare()
    assert dashboard.revision() != initial_revision
    queued = dashboard.list_runs()
    assert len(queued.runs) == 2
    assert {run.campaign_id for run in queued.runs} == {"campaign-live"}
    assert {run.seed for run in queued.runs} == {17}
    assert {run.job_status for run in queued.runs} == {"queued"}
    assert {run.status for run in queued.runs} == {"starting"}
    assert {run.revision for run in queued.runs} == {0}
    assert {run.control_action for run in queued.runs} == {"resume"}
    assert {run.control_label for run in queued.runs} == {"Resume"}

    claimed = application.repository.claim_next(
        application.plan.campaign_id,
        lease_owner="dashboard-test-worker",
        lease_seconds=120,
    )
    assert claimed is not None
    config = _config(application, claimed.run_id)
    assert config.config_hash == claimed.config_hash
    world = MarketWorld(config.scenario)
    initial_state = world.create_initial_state(config.company_ids)
    run = claimed.run_payload
    run_directory = output / "runs" / f"seed-{run.seed}" / lineup_run_slug(run.lineup_id)
    RunStore(run_directory).ensure_manifest(
        RunManifest.create(config, world.state_hash(initial_state))
    )
    checkpoint = RunCheckpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
        phase=CheckpointPhase.READY,
        initial_state=initial_state,
        current_state=initial_state,
        next_window_index=0,
    )
    saved = application.repository.save_checkpoint(
        claimed.task_key,
        lease_owner="dashboard-test-worker",
        expected_revision=claimed.revision,
        checkpoint=checkpoint,
    )
    response = SealedDecision(
        company_id=config.company_ids[0],
        agent_status="sealed",
        decision=CompanyDecision(
            company_id=config.company_ids[0],
            world_version=initial_state.version,
        ),
        error="sk-private-error",
        harness_trace_path="C:/private/trace.jsonl",
    )
    application.repository.stage_decision(
        claimed.task_key,
        lease_owner="dashboard-test-worker",
        expected_revision=saved.revision,
        window_index=0,
        response=response,
    )

    listing = dashboard.list_runs()
    running = next(item for item in listing.runs if item.run_id == claimed.run_id)
    detail = dashboard.get_run(running.run_ref)
    serialized = detail.model_dump_json().casefold()

    assert running.job_status == "running"
    assert running.status == "running"
    assert running.revision == detail.revision
    assert detail.control_action == "pause"
    assert detail.campaign_id == "campaign-live"
    assert detail.seed == claimed.seed
    assert detail.day == 0
    assert len(detail.companies) == 1
    assert len(detail.companies[0].trajectory) == 1
    assert detail.companies[0].trajectory[0].stage == "collecting"
    assert "task-" not in serialized
    assert "lease_owner" not in serialized
    assert "private_chain" not in serialized
    assert "sk-private" not in serialized
    assert "c:/private" not in serialized
    assert str(tmp_path).casefold() not in serialized
    current = application.repository.get_job(claimed.task_key)
    assert current is not None
    application.repository.renew_lease(
        claimed.task_key,
        lease_owner="dashboard-test-worker",
        expected_revision=current.revision,
        lease_seconds=120,
    )

    def reject_snapshot_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("lease-only changes must not invalidate economic cache")

    with monkeypatch.context() as patch:
        patch.setattr(RunCheckpoint, "model_validate_json", reject_snapshot_parse)
        renewed = dashboard.get_run(running.run_ref)
    assert renewed.revision > detail.revision
    assert renewed.economic_revision == detail.economic_revision
    assert renewed.companies == detail.companies


def test_dashboard_distinguishes_seed_waiting_from_current_seed_interruption(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    output = runs_root / "campaign-seed-wave"
    application = _application(
        _spec("campaign-seed-wave", days=7, seeds=(11, 29)),
        output,
    )
    application.prepare()
    claimed = application.repository.claim_next(
        application.plan.campaign_id,
        lease_owner="retired-seed-29-worker",
        lease_seconds=3600,
        seed=29,
    )
    assert claimed is not None

    dashboard = RunDashboard(runs_root)
    live = next(run for run in dashboard.list_runs().runs if run.run_id == claimed.run_id)
    assert live.status == "running"

    with sqlite3.connect(application.database_path) as connection:
        connection.execute(
            "UPDATE run_jobs SET lease_until = ? WHERE task_key = ?",
            (datetime(2020, 1, 1, tzinfo=UTC).isoformat(), claimed.task_key),
        )

    waiting = tuple(run for run in dashboard.list_runs().runs if run.seed == 29)
    assert {run.status for run in waiting} == {"waiting_seed"}
    assert {run.phase for run in waiting} == {"waiting_previous_seed"}
    assert {run.control_action for run in waiting} == {"none"}
    assert {run.control_label for run in waiting} == {"Waiting for the previous seed"}
    assert {run.error for run in waiting} == {None}

    with sqlite3.connect(application.database_path) as connection:
        connection.execute(
            "UPDATE run_jobs SET status = 'failed', revision = revision + 1 "
            "WHERE campaign_id = ? AND seed = 11",
            (application.plan.campaign_id,),
        )

    current = next(run for run in dashboard.list_runs().runs if run.run_id == claimed.run_id)
    assert current.job_status == "running"
    assert current.status == "interrupted"
    assert current.control_action == "resume"
    assert current.error == "Campaign Run lease expired and is recoverable."


def test_dashboard_seed_priority_keeps_later_seed_running_and_labels_queue_and_retry(
    tmp_path: Path,
) -> None:
    application = _application(
        _spec("campaign-priority", seeds=(11, 29), schedule="seed_priority"),
        tmp_path / "campaign-priority",
    )
    application.prepare()
    first = application.repository.claim_next(
        application.plan.campaign_id,
        lease_owner="retry-worker",
        lease_seconds=3600,
        seed=11,
    )
    later = application.repository.claim_next(
        application.plan.campaign_id,
        lease_owner="live-worker",
        lease_seconds=3600,
        seed=29,
    )
    assert first is not None and later is not None
    application.repository.interrupt_run(
        first.task_key,
        lease_owner="retry-worker",
        expected_revision=first.revision,
        error="test retry",
        retry_delay_seconds=300,
    )

    runs = RunDashboard(tmp_path).list_runs().runs

    assert next(run for run in runs if run.run_id == later.run_id).status == "running"
    assert next(run for run in runs if run.run_id == first.run_id).status == "waiting_retry"
    assert {run.status for run in runs if run.job_status == "queued"} == {"queued"}
    assert all(run.status != "waiting_seed" for run in runs)


def _write_scheduler(application: CampaignApplication, timestamp: datetime) -> Path:
    snapshot = SchedulerSnapshot(
        timestamp=timestamp,
        scheduler_running=True,
        run_limit=1,
        decision_limit=1,
        max_runs=2,
        max_decisions=2,
        active_runs=2,
        active_decisions=2,
        resident_workers=2,
        waiting_decisions=3,
        sample_interval_seconds=10,
        allow_new_runs=False,
        resource_status="pressure",
        reasons=("low_memory",),
        adjustment="reduce",
        resource=ResourceSample(
            timestamp=timestamp,
            available_memory_mb=1234,
            probe_error="PRIVATE_PROBE_ERROR sk-private-key",
        ),
        health=DecisionHealth(),
    )
    path = application.output / "scheduler_status.json"
    path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    return path


def test_dashboard_resources_refresh_expire_and_preserve_actual_over_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(_spec("campaign-resources"), tmp_path / "campaign-resources")
    application.prepare()
    dashboard = RunDashboard(tmp_path)
    assert dashboard.list_runs().resources[0].scheduler is None
    initial_revision = dashboard.revision()
    sampled_at = datetime.now(UTC)
    _write_scheduler(application, sampled_at)

    listing = dashboard.list_runs()
    resource = listing.resources[0]
    assert resource.campaign_id == "campaign-resources"
    assert resource.scheduler is not None
    assert resource.scheduler.active_runs == resource.scheduler.active_decisions == 2
    assert resource.scheduler.resident_workers == 2
    assert resource.scheduler.waiting_decisions == 3
    assert resource.scheduler.run_limit == resource.scheduler.decision_limit == 1
    assert resource.scheduler.available_memory_mb == 1234
    assert "private" not in listing.model_dump_json().casefold()
    assert "probe_error" not in listing.model_dump_json()
    live_revision = dashboard.revision()
    assert live_revision != initial_revision

    class _ExpiredClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            del tz
            return sampled_at + timedelta(seconds=31)

    monkeypatch.setattr(campaign_reader, "datetime", _ExpiredClock)
    assert dashboard.revision() != live_revision
    assert dashboard.list_runs().resources[0].scheduler is None
    assert dashboard.list_runs().runs == listing.runs


@pytest.mark.parametrize(
    "payload",
    ["{}", "broken json", " " * 64_001],
    ids=["missing_fields", "invalid_json", "oversized"],
)
def test_dashboard_invalid_or_oversized_resources_do_not_hide_campaign(
    tmp_path: Path,
    payload: str,
) -> None:
    application = _application(_spec("campaign-safe-resource"), tmp_path / "campaign-safe-resource")
    application.prepare()
    (application.output / "scheduler_status.json").write_text(payload, encoding="utf-8")

    listing = RunDashboard(tmp_path).list_runs()

    assert len(listing.runs) == 2
    assert listing.resources[0].scheduler is None


def test_dashboard_projects_completed_campaign_and_score_only_fallback(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    output = runs_root / "campaign-complete"
    application = _application(_spec("campaign-complete", days=1), output)
    report = asyncio.run(application.run())
    assert report.campaign_status == "completed"
    assert report.status_counts == {"completed": 2}

    dashboard = RunDashboard(runs_root)
    listing = dashboard.list_runs()
    assert len(listing.runs) == 2
    assert {run.status for run in listing.runs} == {"complete"}
    assert {run.control_action for run in listing.runs} == {"none"}
    assert {run.control_label for run in listing.runs} == {"Completed"}
    details = [dashboard.get_run(run.run_ref) for run in listing.runs]
    assert all(detail.detail_available for detail in details)
    assert all(detail.companies[0].trajectory for detail in details)
    assert all(detail.companies[0].cash_cents is not None for detail in details)

    missing = application.plan.runs[0]
    missing_path = output / "runs" / f"seed-{missing.seed}" / lineup_run_slug(missing.lineup_id)
    (missing_path / "final_checkpoint.json").unlink()
    with sqlite3.connect(application.database_path) as connection:
        connection.execute("DELETE FROM checkpoints WHERE task_key = ?", (missing.task_key,))
    fallback_dashboard = RunDashboard(runs_root)
    fallback_summary = next(
        item for item in fallback_dashboard.list_runs().runs if item.run_id == missing.run_id
    )
    fallback = fallback_dashboard.get_run(fallback_summary.run_ref)

    assert fallback.status == "complete"
    assert fallback.phase == "verified_result_only"
    assert not fallback.detail_available
    assert fallback.companies[0].score_cents != 0
    assert fallback.companies[0].cash_cents is None
    assert fallback.total_cash_cents is None
    assert fallback.companies[0].trajectory == ()

    tampered = application.plan.runs[1]
    tampered_path = output / "runs" / f"seed-{tampered.seed}" / lineup_run_slug(tampered.lineup_id)
    (tampered_path / "final_checkpoint.json").write_text("{}", encoding="utf-8")
    with (tampered_path / "trace.jsonl").open("ab") as stream:
        stream.write(b"\n")
    tampered_dashboard = RunDashboard(runs_root)
    tampered_summary = next(
        item for item in tampered_dashboard.list_runs().runs if item.run_id == tampered.run_id
    )
    tampered_detail = tampered_dashboard.get_run(tampered_summary.run_ref)

    assert tampered_detail.status == "complete"
    assert tampered_detail.phase == "complete"
    assert tampered_detail.detail_available
    assert tampered_detail.error is not None
    assert tampered_detail.recent_events == ()


def test_dashboard_isolates_bad_campaign_and_never_mutates_sqlite(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    good = _application(_spec("campaign-good"), runs_root / "good")
    bad = _application(_spec("campaign-bad"), runs_root / "bad")
    good.prepare()
    bad.prepare()
    with sqlite3.connect(bad.database_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    tracked = tuple(
        path
        for path in good.output.iterdir()
        if path.name.startswith("campaign.sqlite3") or path.name == "campaign_plan.json"
    )
    names_before = {path.name for path in good.output.iterdir()}
    before = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in tracked}
    dashboard = RunDashboard(runs_root)

    listing = dashboard.list_runs()
    dashboard.revision()
    after = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in tracked}
    names_after = {path.name for path in good.output.iterdir()}

    assert len(listing.runs) == 2
    assert all(run.run_id in {item.run_id for item in good.plan.runs} for run in listing.runs)
    assert before == after
    assert names_before == names_after


def test_dashboard_reads_committed_snapshot_while_wal_writer_is_active(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    application = _application(_spec("campaign-wal"), runs_root / "campaign-wal")
    application.prepare()
    writer = sqlite3.connect(application.database_path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE run_jobs SET current_day = 1 WHERE campaign_id = ?",
            (application.plan.campaign_id,),
        )

        listing = RunDashboard(runs_root).list_runs()

        assert len(listing.runs) == 2
        assert {run.day for run in listing.runs} == {0}
    finally:
        writer.rollback()
        writer.close()
