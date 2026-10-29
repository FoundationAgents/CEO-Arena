"""Completed outcomes survive a provenance-only recovery upgrade without new inference."""

import json
import sqlite3
from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication, load_campaign_plan
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.http400_migration import (
    Http400RetryMigration,
    Http400ThreeAttemptMigration,
)
from ceo_arena.campaign.records import RunJobStatus
from ceo_arena.campaign.recovery_migration import RecoveryPatchMigration
from ceo_arena.evaluation import ArenaRunResult
from ceo_arena.runtime.admission import UnlimitedDecisionAdmission
from ceo_arena.runtime.persistence import RunManifest

from .test_application import _ModelHarness, _spec

_FILES = ("src/ceo_arena/runtime/coordinator.py", "src/ceo_arena/runtime/retry.py")


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_type", [Http400RetryMigration, Http400ThreeAttemptMigration])
async def test_completed_run_upgrade_preserves_scores_traces_and_original_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration_type: type[Http400RetryMigration],
) -> None:
    before = dict.fromkeys(_FILES, "a" * 64)
    after = dict.fromkeys(_FILES, "b" * 64)
    if migration_type is Http400ThreeAttemptMigration:
        after[_FILES[0]] = before[_FILES[0]]
    monkeypatch.setattr(
        "ceo_arena.campaign.recovery_migration.simulation_fingerprints", lambda _: after
    )
    spec = _spec()
    plan = CampaignCompiler().compile(spec, simulation_fingerprints=before)
    app = CampaignApplication(
        spec,
        plan,
        tmp_path / "campaign",
        model_harness_builder=lambda route, _: _ModelHarness(route.model_id),
    )
    app.prepare()
    for run in plan.runs:
        job = app.repository.claim_task(run.task_key, lease_owner="offline", lease_seconds=120)
        assert job is not None
        await app._run_claimed(job, "offline", UnlimitedDecisionAdmission())
        assert app.repository.get_job(run.task_key).status is RunJobStatus.COMPLETED
    app._export_final_checkpoints()
    originals = {r.run_id: r for r in app.repository.list_results(spec.campaign_id)}
    original_plan_bytes = app.plan_path.read_bytes()
    traces = {r.run_id: (app._run_directory(r) / "trace.jsonl").read_bytes() for r in plan.runs}
    with pytest.raises(ValueError, match="published results"):
        RecoveryPatchMigration(app.output).run()
    migration = migration_type(app.output)
    preview = migration.run()
    assert not preview.applied
    assert app.plan_path.read_bytes() == original_plan_bytes
    receipt = migration.run(apply=True, expected_old_plan_hash=plan.plan_hash)
    assert receipt.applied
    assert receipt.details["http400_max_attempts"] == (
        3 if migration_type is Http400ThreeAttemptMigration else 10
    )
    assert len(receipt.details["completed_runs_not_reexecuted"]) == len(plan.runs)
    new_plan = load_campaign_plan(app.output)
    new_app = CampaignApplication(spec, new_plan, app.output)
    new_app.prepare()
    new_app._verify_published_runs()
    assert len(new_app._build_analysis().track_a) == 3
    for run in plan.runs:
        directory = app._run_directory(run)
        assert (directory / "trace.jsonl").read_bytes() == traces[run.run_id]
        result = next(
            r for r in new_app.repository.list_results(spec.campaign_id) if r.run_id == run.run_id
        )
        assert result.model_dump(exclude={"config_hash", "artifact_fingerprints"}) == originals[
            run.run_id
        ].model_dump(exclude={"config_hash", "artifact_fingerprints"})
        assert RunManifest.model_validate_json(
            (directory / "manifest.json").read_bytes()
        ).config == new_app._run_config(run)
    backup = app.output / "recovery-patches" / f"{migration.patch_id}-{plan.plan_hash[:12]}"
    with sqlite3.connect(backup / "campaign.sqlite3") as connection:
        saved = [
            ArenaRunResult.model_validate_json(row[0])
            for row in connection.execute("SELECT payload_json FROM run_results")
        ]
    assert {r.run_id: r for r in saved} == originals
    assert json.loads((backup / "receipt.json").read_bytes())["applied"]
    with pytest.raises(ValueError):
        migration.run()


def test_http400_upgrade_rejects_economic_or_other_harness_changes() -> None:
    before = dict.fromkeys(_FILES, "a" * 64)
    after = dict.fromkeys(_FILES, "b" * 64)
    assert len(Http400RetryMigration._fingerprint_changes(before, after)) == 2
    for path in ("src/ceo_arena/economy/world.py", "agent_worker/src/runner.ts"):
        with pytest.raises(ValueError):
            Http400RetryMigration._fingerprint_changes(before, {**after, path: "c" * 64})


def test_three_attempt_upgrade_allows_only_retry_policy_change() -> None:
    before = dict.fromkeys(_FILES, "a" * 64)
    after = {**before, _FILES[1]: "b" * 64}
    assert len(Http400ThreeAttemptMigration._fingerprint_changes(before, after)) == 1
    for changed in (before, {**after, _FILES[0]: "c" * 64}, {**after, "economy": "c" * 64}):
        with pytest.raises(ValueError):
            Http400ThreeAttemptMigration._fingerprint_changes(before, changed)
