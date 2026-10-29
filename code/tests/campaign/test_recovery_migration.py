from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest

from ceo_arena.campaign.application import CampaignApplication, load_campaign_plan
from ceo_arena.campaign.checkpoint import CampaignCheckpointStore
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.models import FrozenCampaignPlan
from ceo_arena.campaign.records import RunJobStatus, RunStopReason
from ceo_arena.campaign.recovery_migration import (
    INCOMPLETE_MARKER,
    RecoveryPatchMigration,
    assert_recovery_ready,
)
from ceo_arena.campaign.sol_alias_migration import SOL_ALIAS, SOL_MODEL, SolAliasMigration
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.economy import MarketWorld
from ceo_arena.harness.wire import WorkerDecisionRequest
from ceo_arena.models import (
    AgentUsage,
    CompanyDecision,
    DecisionWindowRequest,
    ProviderFailure,
    WindowResult,
    WorldState,
)
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.allowance import AllowanceLedger
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.persistence import (
    CheckpointPhase,
    CollectingWindow,
    CompletedWindow,
    DayFingerprint,
    RunCheckpoint,
    RunManifest,
    SealedDecision,
)

_RUNNER = "agent_worker/src/runner.ts"
_OLD_FINGERPRINTS = {_RUNNER: "a" * 64, "src/ceo_arena/economy/world.py": "e" * 64}
_NEW_FINGERPRINTS = {**_OLD_FINGERPRINTS, _RUNNER: "b" * 64}


@pytest.fixture
def campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> CampaignApplication:
    monkeypatch.setattr(
        "ceo_arena.campaign.recovery_migration.simulation_fingerprints",
        lambda _: _NEW_FINGERPRINTS,
    )
    spec = ExperimentSpec(
        campaign_id="offline-recovery-patch",
        base_url="https://unused.example/v1",
        company_count=2,
        days=21,
        seeds=(11,),
        api=(
            ApiSpec(
                api_key="never-transmitted",
                models=("model-a", getattr(request, "param", "model-b")),
            ),
        ),
    )
    plan = CampaignCompiler().compile(spec, simulation_fingerprints=_OLD_FINGERPRINTS)
    app = CampaignApplication(spec, plan, tmp_path / "campaign")
    app.prepare()
    for run in plan.runs:
        config = app._run_config(run)
        directory = app._run_directory(run)
        directory.mkdir(parents=True)
        world = MarketWorld(config.scenario)
        initial = world.create_initial_state(run.company_ids)
        responses = tuple(
            SealedDecision(
                company_id=company,
                agent_status="sealed",
                decision=CompanyDecision(company_id=company, world_version=initial.version),
                usage=AgentUsage(input_tokens=117, output_tokens=12),
            )
            for company in run.company_ids
        )
        advanced = world.advance_window(
            initial.model_copy(deep=True), {item.company_id: item.decision for item in responses}
        )
        completed = CompletedWindow(
            window_index=0,
            start_day=0,
            start_world_version=initial.version,
            start_state_hash=world.state_hash(initial),
            decisions=responses,
            end_day=advanced.end_day,
            end_world_version=advanced.world_version,
            end_state_hash=world.state_hash(advanced.state),
            day_fingerprints=tuple(
                DayFingerprint(
                    day=day.day, world_version=day.world_version, state_hash=day.state_hash
                )
                for day in advanced.days
            ),
        )
        pending_response = SealedDecision(
            company_id=run.company_ids[0],
            agent_status="sealed",
            decision=CompanyDecision(
                company_id=run.company_ids[0], world_version=advanced.world_version
            ),
            usage=AgentUsage(input_tokens=51, output_tokens=7),
        )
        checkpoint = RunCheckpoint(
            run_id=run.run_id,
            config_hash=config.config_hash,
            phase=CheckpointPhase.COLLECTING_DECISIONS,
            initial_state=initial,
            current_state=advanced.state,
            next_window_index=1,
            completed_windows=(completed,),
            collecting_window=CollectingWindow(
                window_index=1,
                start_day=advanced.end_day,
                start_world_version=advanced.world_version,
                start_state_hash=world.state_hash(advanced.state),
                expected_company_ids=run.company_ids,
                decisions=(pending_response,),
            ),
        )
        AtomicModelFile(directory / "manifest.json", RunManifest).save(
            RunManifest.create(config, world.state_hash(initial))
        )
        job = app.repository.claim_task(run.task_key, lease_owner="offline", lease_seconds=120)
        assert job is not None
        job = app.repository.save_checkpoint(
            run.task_key,
            lease_owner="offline",
            expected_revision=job.revision,
            checkpoint=checkpoint,
        )
        app.repository.stop_run(
            run.task_key,
            lease_owner="offline",
            expected_revision=job.revision,
            reason=RunStopReason.USER_REQUESTED,
            error="offline fixture paused",
        )
        allowance = AllowanceLedger(
            directory, config.config_hash, run.company_ids[1], advanced.world_version
        )
        allowance.save(
            AgentUsage(input_tokens=789, latency_ms=231000), pending_response.diagnostics
        )
        (directory / "agent-traces").mkdir()
        (directory / "agent-memory").mkdir()
        (directory / "agent-memory" / "conversation.json").write_text(
            '{"messages":[{"content":"retain draft and notes"}]}', encoding="utf-8"
        )
        (directory / "trace.jsonl").write_text("unchanged raw audit\n", encoding="utf-8")
    return app


def test_dry_run_proves_prefix_without_changing_bindings(campaign: CampaignApplication) -> None:
    before = campaign.plan_path.read_bytes()
    jobs = campaign.repository.list_jobs(campaign.plan.campaign_id)
    report = RecoveryPatchMigration(campaign.output).run()
    assert not report.applied
    assert report.old_plan_hash == campaign.plan.plan_hash
    assert report.new_plan_hash != report.old_plan_hash
    assert {item.day for item in report.runs} == {7}
    assert all(
        item.verified_days == 7 and item.received_decision_count == 1 for item in report.runs
    )
    assert campaign.plan_path.read_bytes() == before
    assert campaign.repository.list_jobs(campaign.plan.campaign_id) == jobs
    assert not (campaign.output / "recovery-patches").exists()


def test_apply_preserves_days_decisions_usage_and_raw_traces(campaign: CampaignApplication) -> None:
    originals = {
        run.task_key: campaign.repository.load_recovery(run.task_key) for run in campaign.plan.runs
    }
    report = RecoveryPatchMigration(campaign.output).run(
        apply=True, expected_old_plan_hash=campaign.plan.plan_hash
    )
    assert report.applied
    new_plan = load_campaign_plan(campaign.output)
    assert new_plan.plan_hash == report.new_plan_hash
    assert new_plan.simulation_fingerprints == _NEW_FINGERPRINTS
    assert new_plan.model_dump(exclude={"simulation_fingerprints"}) == campaign.plan.model_dump(
        exclude={"simulation_fingerprints"}
    )
    assert not (campaign.output / INCOMPLETE_MARKER).exists()
    backup = next((campaign.output / "recovery-patches").iterdir())
    with sqlite3.connect(backup / "campaign.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("SELECT plan_hash FROM campaigns").fetchone()[0]
            == campaign.plan.plan_hash
        )
    for proof, run in zip(report.runs, campaign.plan.runs, strict=True):
        old = originals[run.task_key]
        new = campaign.repository.load_recovery(run.task_key)
        assert old is not None and new is not None
        assert old.checkpoint is not None and new.checkpoint is not None
        assert old.decisions == new.decisions
        assert old.checkpoint.model_dump(exclude={"config_hash"}) == new.checkpoint.model_dump(
            exclude={"config_hash"}
        )
        assert new.job.status is RunJobStatus.STOPPED
        assert new.job.run_id == old.job.run_id
        assert new.job.config_hash == proof.new_config_hash
        directory = campaign._run_directory(run)
        value = AllowanceLedger(directory, proof.new_config_hash, run.company_ids[1], 7).load()
        assert value.usage.input_tokens == 789 and value.usage.latency_ms == 231000
        manifest = RunManifest.model_validate_json((directory / "manifest.json").read_bytes())
        assert manifest.config_hash == proof.new_config_hash
        assert (directory / "trace.jsonl").read_text() == "unchanged raw audit\n"
    CampaignApplication(campaign.spec, new_plan, campaign.output).prepare()


def test_apply_requires_reviewed_old_hash(campaign: CampaignApplication) -> None:
    with pytest.raises(ValueError, match="exact reviewed"):
        RecoveryPatchMigration(campaign.output).run(apply=True)


@pytest.mark.parametrize(
    "path", ["src/ceo_arena/economy/world.py", "python_version", "src/ceo_arena/config.py"]
)
def test_refuses_non_harness_changes(
    campaign: CampaignApplication, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(
        "ceo_arena.campaign.recovery_migration.simulation_fingerprints",
        lambda _: {**_NEW_FINGERPRINTS, path: "forbidden"},
    )
    with pytest.raises(ValueError, match="unexpected changes"):
        RecoveryPatchMigration(campaign.output).run()


def test_refuses_running_leases(campaign: CampaignApplication) -> None:
    job = campaign.repository.list_jobs(campaign.plan.campaign_id)[0]
    campaign.repository.resume_recoverable(job.task_key, expected_revision=job.revision)
    campaign.repository.claim_task(job.task_key, lease_owner="live", lease_seconds=120)
    with pytest.raises(ValueError, match="stop all Runs"):
        RecoveryPatchMigration(campaign.output).run()


def test_refuses_active_process_lock(campaign: CampaignApplication) -> None:
    with (
        RunDirectoryLock(campaign.output / ".campaign.lock"),
        pytest.raises(RuntimeError, match="already active"),
    ):
        RecoveryPatchMigration(campaign.output).run()


def test_refuses_published_results(campaign: CampaignApplication) -> None:
    job = campaign.repository.list_jobs(campaign.plan.campaign_id)[0]
    with sqlite3.connect(campaign.database_path) as connection:
        connection.execute(
            "INSERT INTO run_results VALUES (?,?,?,?)", (job.task_key, "hash", "{}", "2026-09-13")
        )
    with pytest.raises(ValueError, match="published results"):
        RecoveryPatchMigration(campaign.output).run()


def test_refuses_changed_allowance_binding(campaign: CampaignApplication) -> None:
    path = next(campaign.output.glob("runs/**/allowances/*.json"))
    value = json.loads(path.read_bytes())
    value["config_hash"] = "invalid"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="allowance binding"):
        RecoveryPatchMigration(campaign.output).run()


def test_crash_during_publication_leaves_marker_and_backup(
    campaign: CampaignApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_verify(*_: object) -> None:
        raise RuntimeError("injected post-file publication failure")

    monkeypatch.setattr(RecoveryPatchMigration, "_verify_preserved", fail_verify)
    with pytest.raises(RuntimeError, match="injected"):
        RecoveryPatchMigration(campaign.output).run(
            apply=True, expected_old_plan_hash=campaign.plan.plan_hash
        )
    assert (campaign.output / INCOMPLETE_MARKER).exists()
    with pytest.raises(RuntimeError, match="incomplete"):
        assert_recovery_ready(campaign.output)
    with pytest.raises(RuntimeError, match="incomplete"):
        CampaignApplication(
            campaign.spec, load_campaign_plan(campaign.output), campaign.output
        ).prepare()
    with sqlite3.connect(campaign.database_path) as connection:
        assert (
            connection.execute("SELECT plan_hash FROM campaigns").fetchone()[0]
            == campaign.plan.plan_hash
        )
    backup = next((campaign.output / "recovery-patches").iterdir())
    assert (
        FrozenCampaignPlan.model_validate_json(
            (backup / "original" / "campaign_plan.json").read_bytes()
        ).plan_hash
        == campaign.plan.plan_hash
    )


def test_fails_exact_replay_if_economy_changed(
    campaign: CampaignApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = MarketWorld.advance_window

    def altered(
        self: MarketWorld, state: WorldState, decisions: Mapping[str, CompanyDecision]
    ) -> WindowResult:
        result = original(self, state, decisions)
        result.state.companies["company-1"].cash_cents += 1
        return result

    monkeypatch.setattr(MarketWorld, "advance_window", altered)
    with pytest.raises(AssertionError, match="state"):
        RecoveryPatchMigration(campaign.output).run()


def test_unmerged_sealed_response_remains_available_without_provider_call(
    campaign: CampaignApplication,
) -> None:
    run = campaign.plan.runs[0]
    recovery = campaign.repository.load_recovery(run.task_key)
    assert recovery is not None and recovery.checkpoint is not None
    window = recovery.checkpoint.collecting_window
    assert window is not None
    campaign.repository.resume_recoverable(run.task_key, expected_revision=recovery.job.revision)
    job = campaign.repository.claim_task(run.task_key, lease_owner="offline", lease_seconds=120)
    assert job is not None
    response = SealedDecision(
        company_id=run.company_ids[1],
        agent_status="sealed",
        decision=CompanyDecision(
            company_id=run.company_ids[1], world_version=window.start_world_version
        ),
        usage=AgentUsage(input_tokens=321),
    )
    job = campaign.repository.stage_decision(
        run.task_key,
        lease_owner="offline",
        expected_revision=job.revision,
        window_index=window.window_index,
        response=response,
        window=window,
        run_id=run.run_id,
        config_hash=job.config_hash,
    )
    job = campaign.repository.stop_run(
        run.task_key,
        lease_owner="offline",
        expected_revision=job.revision,
        reason=RunStopReason.USER_REQUESTED,
        error="offline staged result",
    )
    receipt = RecoveryPatchMigration(campaign.output).run(
        apply=True, expected_old_plan_hash=campaign.plan.plan_hash
    )
    assert receipt.runs[0].received_decision_count == 2
    job = campaign.repository.get_job(run.task_key)
    assert job is not None
    campaign.repository.resume_recoverable(run.task_key, expected_revision=job.revision)
    job = campaign.repository.claim_task(run.task_key, lease_owner="resumed", lease_seconds=120)
    assert job is not None
    merged = CampaignCheckpointStore(
        campaign.repository, task_key=run.task_key, lease_owner="resumed", revision=job.revision
    ).load(run_id=run.run_id, config_hash=job.config_hash)
    assert merged is not None and merged.collecting_window is not None
    assert response in merged.collecting_window.decisions
    assert len(merged.collecting_window.decisions) == 2


def test_detects_memory_mutation_before_committing(
    campaign: CampaignApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RecoveryPatchMigration._verify_preserved
    memory = next(campaign.output.glob("runs/**/agent-memory/conversation.json"))

    def altered(self: RecoveryPatchMigration, connection: sqlite3.Connection, run) -> None:
        memory.write_text("changed draft", encoding="utf-8")
        original(self, connection, run)

    monkeypatch.setattr(RecoveryPatchMigration, "_verify_preserved", altered)
    with pytest.raises(ValueError, match="raw traces changed"):
        RecoveryPatchMigration(campaign.output).run(
            apply=True, expected_old_plan_hash=campaign.plan.plan_hash
        )
    assert (campaign.output / INCOMPLETE_MARKER).exists()


@pytest.fixture
def alias_migration(
    campaign: CampaignApplication, monkeypatch: pytest.MonkeyPatch
) -> SolAliasMigration:
    monkeypatch.setattr(
        "ceo_arena.campaign.sol_alias_migration.simulation_fingerprints",
        lambda _: _OLD_FINGERPRINTS,
    )
    updated = campaign.spec.model_copy(
        update={
            "inference": {
                **campaign.spec.inference,
                SOL_MODEL: campaign.spec.inference[SOL_MODEL].model_copy(
                    update={"expected_model_ids": (SOL_MODEL, SOL_ALIAS)}
                ),
            }
        }
    )
    return SolAliasMigration(campaign.output, campaign.spec, updated)


def _sol_journal(campaign: CampaignApplication, migration: SolAliasMigration) -> tuple[Path, dict]:
    run = next(run for run in campaign.plan.runs if run.model_ids == ("model-a", SOL_MODEL))
    company = "company-2"
    recovery = campaign.repository.load_recovery(run.task_key)
    assert recovery is not None and recovery.checkpoint is not None
    state = recovery.checkpoint.current_state
    observation = MarketWorld(campaign._run_config(run).scenario).project_observations(state)[
        company
    ]
    request = WorkerDecisionRequest(
        request_id="offline-test",
        request=DecisionWindowRequest(
            run_id=run.run_id,
            company_id=company,
            world_version=state.version,
            observation=observation,
            budget=campaign.plan.budget,
        ),
    )
    binding = migration._conversation_bindings(request)["before"]
    owner = hashlib.sha256(
        json.dumps([run.run_id, company], separators=(",", ":")).encode()
    ).hexdigest()
    path = campaign._run_directory(run) / "agent-memory" / owner / "runtime/conversation.json"
    path.parent.mkdir(parents=True)
    snapshot = {
        "world_version": state.version,
        "binding": binding,
        "system": [{"text": "保持原文"}],
        "messages": [{"role": "assistant", "content": "draft"}],
        "draft": {"company_id": company},
        "usage": {"input_tokens": 789},
        "pending": {"calls": [], "results": []},
        "status": "budget_exhausted",
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return path, snapshot


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
def test_sol_alias_preserves_journal_except_binding_and_compiles_real_hashes(
    campaign: CampaignApplication,
    alias_migration: SolAliasMigration,
) -> None:
    path, before = _sol_journal(campaign, alias_migration)
    dry = alias_migration.run()
    assert not dry.applied and json.loads(path.read_bytes()) == before
    receipt = alias_migration.run(apply=True, expected_old_plan_hash=campaign.plan.plan_hash)
    after = json.loads(path.read_bytes())
    assert after.pop("binding") != before.pop("binding")
    assert after == before
    assert receipt.patch_id == "sol-return-alias-20260913"
    assert receipt.details["user_authorized_response_ids"] == [SOL_MODEL, SOL_ALIAS]
    plan = load_campaign_plan(campaign.output)
    assert plan.simulation_fingerprints == campaign.plan.simulation_fingerprints
    assert plan.runs == campaign.plan.runs
    CampaignApplication(alias_migration.new_spec, plan, campaign.output).prepare()
    with pytest.raises(ValueError, match="old specification"):
        alias_migration.run()


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
@pytest.mark.parametrize("legacy", [False, True])
def test_sol_alias_requeues_only_exact_identity_failure_preserving_usage_and_history(
    campaign: CampaignApplication,
    alias_migration: SolAliasMigration,
    legacy: bool,
) -> None:
    _sol_journal(campaign, alias_migration)
    sol = next(run for run in campaign.plan.runs if run.model_ids == ("model-a", SOL_MODEL))
    other = next(run for run in campaign.plan.runs if SOL_MODEL not in run.model_ids)
    error = (
        f"ProviderIdentityError: company-2: Requested {SOL_MODEL}; provider reported {SOL_ALIAS}"
    )
    with sqlite3.connect(campaign.database_path) as connection:
        for run, message in ((sol, error), (other, "Qwen HTTP 400")):
            connection.execute(
                "UPDATE run_jobs SET status='failed', stop_reason=NULL, last_error=? "
                "WHERE task_key=?",
                (message, run.task_key),
            )
            connection.execute(
                "INSERT INTO failure_history(task_key,attempt,phase,error,occurred_at) "
                "VALUES (?,1,'provider',?,'2026-09-13')",
                (run.task_key, message),
            )
        history = connection.execute("SELECT * FROM failure_history").fetchall()
    ledger = AllowanceLedger(
        campaign._run_directory(sol), campaign._run_config(sol).config_hash, "company-2", 7
    )
    before = ledger.load()
    ledger.save(
        before.usage,
        before.diagnostics,
        None if legacy else ProviderFailure(category="identity", message=error, retryable=False),
    )
    receipt = alias_migration.run(apply=True, expected_old_plan_hash=campaign.plan.plan_hash)
    proof = next(proof for proof in receipt.runs if proof.task_key == sol.task_key)
    after = AllowanceLedger(
        campaign._run_directory(sol), proof.new_config_hash, "company-2", 7
    ).load()
    assert (
        after.usage == before.usage
        and after.diagnostics == before.diagnostics
        and after.failure is None
    )
    assert campaign.repository.get_job(sol.task_key).status is RunJobStatus.QUEUED
    assert campaign.repository.get_job(other.task_key).status is RunJobStatus.FAILED
    with sqlite3.connect(campaign.database_path) as connection:
        assert connection.execute("SELECT * FROM failure_history").fetchall() == history
    assert receipt.details["resumed_tasks"] == [sol.task_key]
    assert bool(receipt.details["legacy_missing_failure_metadata"]) is legacy


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
@pytest.mark.parametrize(
    "field,value", [("days", 22), ("base_url", "https://other.example/v1"), ("seeds", (29,))]
)
def test_sol_alias_refuses_other_spec_changes(
    campaign: CampaignApplication, alias_migration: SolAliasMigration, field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="outside Sol"):
        SolAliasMigration(
            campaign.output,
            campaign.spec,
            alias_migration.new_spec.model_copy(update={field: value}),
        )


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
@pytest.mark.parametrize("version", [0, 8])
def test_sol_alias_stale_journal_is_untouched_future_journal_rejected(
    campaign: CampaignApplication, alias_migration: SolAliasMigration, version: int
) -> None:
    path, snapshot = _sol_journal(campaign, alias_migration)
    snapshot["world_version"] = version
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    before = path.read_bytes()
    if version == 8:
        with pytest.raises(ValueError, match="ahead"):
            alias_migration.run()
    else:
        receipt = alias_migration.run(apply=True, expected_old_plan_hash=campaign.plan.plan_hash)
        assert receipt.details["unchanged_stale_conversations"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
def test_sol_alias_refuses_invalid_current_conversation_binding(
    campaign: CampaignApplication, alias_migration: SolAliasMigration
) -> None:
    path, snapshot = _sol_journal(campaign, alias_migration)
    snapshot["binding"] = "invalid"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="binding differs"):
        alias_migration.run()


@pytest.mark.parametrize("suffix", [":other", "/other", "-preview", " unexpected"])
def test_sol_alias_never_unlocks_a_different_response_label(suffix: str) -> None:
    body = f"Requested {SOL_MODEL}; provider reported {SOL_ALIAS}"
    failure = ProviderFailure(category="identity", message=body + suffix, retryable=False)
    assert not SolAliasMigration._matches_failure(
        failure, f"ProviderIdentityError: company-2: {body}"
    )


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
def test_sol_alias_refuses_even_secret_only_spec_changes(
    campaign: CampaignApplication,
    alias_migration: SolAliasMigration,
) -> None:
    changed = alias_migration.new_spec.model_copy(
        update={
            "api": (
                ApiSpec(api_key="different-never-transmitted", models=campaign.spec.api[0].models),
            )
        }
    )
    with pytest.raises(ValueError, match="outside Sol"):
        SolAliasMigration(campaign.output, campaign.spec, changed)


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
@pytest.mark.parametrize(
    "field,value",
    [
        ("effort", "low"),
        ("max_output_tokens", 100),
        ("protocol", "openai-compatible-chat"),
    ],
)
def test_sol_alias_refuses_changes_to_sol_generation_settings(
    campaign: CampaignApplication,
    alias_migration: SolAliasMigration,
    field: str,
    value: object,
) -> None:
    inference = {**alias_migration.new_spec.inference}
    inference[SOL_MODEL] = inference[SOL_MODEL].model_copy(update={field: value})
    changed = alias_migration.new_spec.model_copy(update={"inference": inference})
    with pytest.raises(ValueError, match="Sol inference changed"):
        SolAliasMigration(campaign.output, campaign.spec, changed)


@pytest.mark.parametrize("campaign", [SOL_MODEL], indirect=True)
@pytest.mark.parametrize("field", ["draft", "usage"])
def test_sol_alias_semantic_verification_rejects_mutation_and_preserves_marker(
    campaign: CampaignApplication,
    alias_migration: SolAliasMigration,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    path, _ = _sol_journal(campaign, alias_migration)
    original = alias_migration._update_campaign

    def mutate(connection, old_plan, new_plan) -> None:
        original(connection, old_plan, new_plan)
        value = json.loads(path.read_bytes())
        value[field] = {"unexpected_mutation": 1}
        path.write_text(json.dumps(value), encoding="utf-8")

    monkeypatch.setattr(alias_migration, "_update_campaign", mutate)
    with pytest.raises(ValueError, match="contents or allowance usage"):
        alias_migration.run(apply=True, expected_old_plan_hash=campaign.plan.plan_hash)
    assert (campaign.output / INCOMPLETE_MARKER).exists()
    with sqlite3.connect(campaign.database_path) as connection:
        assert (
            connection.execute("SELECT plan_hash FROM campaigns").fetchone()[0]
            == campaign.plan.plan_hash
        )
    assert next((campaign.output / "recovery-patches").glob("*/campaign.sqlite3")).exists()
