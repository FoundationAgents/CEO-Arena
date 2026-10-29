"""Contract tests for durable Campaign lifecycle and recovery."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ceo_arena.campaign.checkpoint import CampaignCheckpointStore
from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.models import (
    DEFAULT_MAX_RUN_ATTEMPTS,
    CampaignRun,
)
from ceo_arena.campaign.records import (
    CampaignStatus,
    DecisionResponseStatus,
    FailurePhase,
    RunJobSpec,
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.campaign.repository import (
    CampaignRepository,
    InvalidTransitionError,
    LeaseLostError,
    RepositoryConflictError,
    RevisionConflictError,
)
from ceo_arena.campaign.spec import ApiSpec, ExperimentSpec
from ceo_arena.config import default_scenario
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import ArenaAnalysis, ArenaRunResult
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import AgentUsage, CompanyDecision, CompanyStatus
from ceo_arena.runtime.persistence import (
    AgentAssignment,
    AtomicCheckpointStore,
    CheckpointPhase,
    CollectingWindow,
    CompletedWindow,
    DayFingerprint,
    PendingWindow,
    RunCheckpoint,
    RunConfig,
    RunManifest,
    SealedDecision,
)
from ceo_arena.runtime.replay import ReplayReceipt

_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_HASH_A = "a" * 64
_HASH_B = "b" * 64


def test_repository_uses_wal_full_sync_and_busy_timeout(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "campaign.sqlite3", busy_timeout_ms=12_345)

    with repository._storage.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 12_345


def test_old_database_is_rejected_without_migrating_user_trajectories(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    original = CampaignRepository(database)
    _prepare(original, attempt_limit=7)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(RuntimeError, match="unsupported"):
        CampaignRepository(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_unlimited_attempts_survive_thirty_failures_and_pause_cancels_backoff(
    tmp_path: Path,
) -> None:
    repository = CampaignRepository(tmp_path / "unlimited.sqlite3")
    spec = _prepare(repository)
    for attempt in range(35):
        at = _NOW + timedelta(seconds=attempt * 2)
        job = _claim(repository, at=at)
        assert job.attempt == attempt + 1 and job.attempt_limit is None
        interrupted = repository.interrupt_run(
            spec.task_key,
            lease_owner="worker",
            expected_revision=job.revision,
            error="transient transport",
            phase=FailurePhase.PROVIDER,
            retry_delay_seconds=1,
            now=at,
        )
    stopped = repository.request_pause(
        spec.task_key,
        expected_revision=interrupted.revision,
        now=_NOW + timedelta(seconds=69),
    )
    assert stopped.status is RunJobStatus.STOPPED
    assert stopped.retry_not_before is None
    assert (
        repository.claim_next(
            "campaign", lease_owner="worker", lease_seconds=30, now=_NOW + timedelta(seconds=400)
        )
        is None
    )


def test_standalone_recovery_includes_journal_ahead_of_snapshot(tmp_path: Path) -> None:
    config = _config()
    checkpoint = _collecting_checkpoint(config, ())
    manifest = RunManifest.create(config, canonical_model_hash(checkpoint.initial_state))
    (tmp_path / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    store.save(checkpoint)
    assert checkpoint.collecting_window is not None
    response = _decision("company-1")
    store.stage_decision(
        run_id=config.run_id,
        config_hash=config.config_hash,
        window=checkpoint.collecting_window,
        response=response,
    )
    assert (
        RunCheckpoint.model_validate_json(
            (tmp_path / "checkpoint.json").read_bytes()
        ).collecting_window.decisions
        == ()
    )
    recovered = store.load(run_id=config.run_id, config_hash=config.config_hash)
    assert recovered is not None and recovered.collecting_window is not None
    assert recovered.collecting_window.decisions == (response,)


def test_concurrent_claim_grants_one_live_lease(tmp_path: Path) -> None:
    database = tmp_path / "claims.sqlite3"
    first = CampaignRepository(database)
    second = CampaignRepository(database)
    spec = _prepare(first)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda item: item[0].claim_next(
                    "campaign", lease_owner=item[1], lease_seconds=30, now=_NOW
                ),
                ((first, "worker-1"), (second, "worker-2")),
            )
        )

    claimed = tuple(item for item in claims if item is not None)
    assert len(claimed) == 1
    assert claimed[0].task_key == spec.task_key
    assert claimed[0].status is RunJobStatus.RUNNING
    assert claimed[0].revision == 1
    assert claimed[0].attempt == 1


def test_claim_never_reclaims_an_excluded_live_task_even_after_lease_expiry(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "active-exclusion.sqlite3")
    spec = _prepare(repository)
    original = repository.claim_next(
        "campaign", lease_owner="scheduler", lease_seconds=30, now=_NOW
    )
    assert original is not None
    later = _NOW + timedelta(seconds=60)

    assert (
        repository.claim_next(
            "campaign",
            lease_owner="scheduler",
            lease_seconds=30,
            exclude_task_keys=(spec.task_key,),
            now=later,
        )
        is None
    )
    assert repository.get_job(spec.task_key) == original
    recovered = repository.claim_next(
        "campaign", lease_owner="replacement", lease_seconds=30, now=later
    )
    assert recovered is not None and recovered.attempt == 2


def test_claim_next_can_filter_one_seed_without_changing_default_order(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "seed-wave.sqlite3")
    plan = CampaignCompiler().compile(
        ExperimentSpec(
            campaign_id="seed-wave",
            base_url="https://newapi.example/v1",
            company_count=2,
            days=14,
            seeds=(7, 11),
            api=(ApiSpec(api_key="temporary-test-key", models=("model-1", "model-2")),),
        )
    )
    runs = tuple(run for run in plan.runs if run.model_ids == ("model-1", "model-2"))
    repository.create_campaign(
        campaign_id=plan.campaign_id,
        spec_hash=_HASH_A,
        plan_hash=plan.plan_hash,
        expected_run_count=len(runs),
        now=_NOW,
    )
    specs = tuple(
        RunJobSpec.create(
            campaign_id=run.campaign_id,
            run_id=run.run_id,
            lineup_id=run.lineup_id,
            seed=run.seed,
            config_hash=canonical_model_hash(run),
            run_payload_json=run.model_dump_json(),
        )
        for run in runs
    )
    repository.enqueue_runs(
        specs,
        attempt_limit=plan.execution.max_run_attempts,
        now=_NOW,
    )

    selected = repository.claim_next(
        plan.campaign_id,
        lease_owner="seed-11-worker",
        lease_seconds=30,
        seed=11,
        now=_NOW,
    )
    default = repository.claim_next(
        plan.campaign_id,
        lease_owner="default-worker",
        lease_seconds=30,
        now=_NOW,
    )

    assert selected is not None and selected.seed == 11
    assert default is not None and default.seed == 7


def test_expired_lease_is_auto_resumable_and_can_be_taken_over(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "takeover.sqlite3")
    spec = _prepare(repository)
    first = _claim(repository, owner="dead-worker", at=_NOW, lease_seconds=5)

    assert repository.list_auto_resume_jobs("campaign", now=_NOW + timedelta(seconds=4)) == ()
    assert repository.list_auto_resume_jobs("campaign", now=_NOW + timedelta(seconds=5)) == (first,)

    takeover = repository.claim_next(
        "campaign",
        lease_owner="replacement-worker",
        lease_seconds=30,
        now=_NOW + timedelta(seconds=5),
    )
    assert takeover is not None
    assert takeover.task_key == spec.task_key
    assert takeover.lease_owner == "replacement-worker"
    assert takeover.attempt == 2
    assert takeover.revision == 2

    with pytest.raises((LeaseLostError, RevisionConflictError)):
        repository.renew_lease(
            spec.task_key,
            lease_owner="dead-worker",
            expected_revision=first.revision,
            lease_seconds=30,
            now=_NOW + timedelta(seconds=5),
        )


def test_stage_and_checkpoint_transaction_rolls_back_together(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "rollback.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)
    response = _decision("company-2")
    staged = repository.stage_decision(
        spec.task_key,
        lease_owner="worker",
        expected_revision=claimed.revision,
        window_index=0,
        response=response,
        now=_NOW + timedelta(seconds=1),
    )
    conflicting = response.model_copy(update={"usage": AgentUsage(tool_calls=1)})
    checkpoint = _collecting_checkpoint(
        _config(),
        (_decision("company-1"), conflicting),
    )
    with pytest.raises(RepositoryConflictError, match="different content"):
        repository.save_checkpoint(
            spec.task_key,
            lease_owner="worker",
            expected_revision=staged.revision,
            checkpoint=checkpoint,
            now=_NOW + timedelta(seconds=2),
        )

    recovery = repository.load_recovery(spec.task_key)
    assert recovery is not None
    assert recovery.job.revision == staged.revision
    assert recovery.checkpoint is None
    assert tuple(item.company_id for item in recovery.decisions) == ("company-2",)
    assert recovery.decisions[0].status is DecisionResponseStatus.SEALED


def test_checkpoint_and_staged_decision_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "reopen.sqlite3"
    repository = CampaignRepository(database)
    spec = _prepare(repository)
    claimed = _claim(repository)
    store = CampaignCheckpointStore(
        repository,
        task_key=spec.task_key,
        lease_owner="worker",
        revision=claimed.revision,
        clock=lambda: _NOW + timedelta(seconds=1),
    )
    config = _config()
    store.save(_ready_checkpoint(config))
    partial = _collecting_checkpoint(config, (_decision("company-1"),))
    store.save(partial)

    reopened = CampaignRepository(database)
    reopened_store = CampaignCheckpointStore(
        reopened,
        task_key=spec.task_key,
        lease_owner="worker",
        revision=store.revision,
        clock=lambda: _NOW + timedelta(seconds=2),
    )
    assert reopened_store.load(run_id=config.run_id, config_hash=config.config_hash) == partial

    collecting = _collecting_checkpoint(
        config,
        (_decision("company-1"), _decision("company-2")),
    )
    reopened_store.save(collecting)
    sealed = _sealed_checkpoint(collecting)
    reopened_store.save(sealed)
    committed = _committed_checkpoint(config, sealed)
    reopened_store.save(committed)

    recovery = reopened.load_recovery(spec.task_key)
    assert recovery is not None
    assert recovery.job.revision == reopened_store.revision
    assert recovery.checkpoint == committed
    assert len(recovery.decisions) == 2
    assert {item.status for item in recovery.decisions} == {DecisionResponseStatus.APPLIED}


def test_checkpoint_adapter_reuses_a_response_staged_ahead_of_checkpoint(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "staged-ahead.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)
    config = _config()
    store = CampaignCheckpointStore(
        repository,
        task_key=spec.task_key,
        lease_owner="worker",
        revision=claimed.revision,
        clock=lambda: _NOW + timedelta(seconds=1),
    )
    store.save(_ready_checkpoint(config))
    window = _collecting_checkpoint(config, ()).collecting_window
    assert window is not None
    store.stage_decision(
        run_id=config.run_id,
        config_hash=config.config_hash,
        window=window,
        response=_decision("company-1"),
    )
    resumed = CampaignCheckpointStore(
        repository,
        task_key=spec.task_key,
        lease_owner="worker",
        revision=store.revision,
        clock=lambda: _NOW + timedelta(seconds=3),
    ).load(run_id=config.run_id, config_hash=config.config_hash)

    assert resumed is not None
    assert resumed.phase is CheckpointPhase.COLLECTING_DECISIONS
    assert resumed.collecting_window is not None
    assert resumed.collecting_window.decisions == (_decision("company-1"),)


def test_revision_cas_and_failure_history_are_durable_and_redacted(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "failures.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)

    with pytest.raises(RevisionConflictError):
        repository.interrupt_run(
            spec.task_key,
            lease_owner="worker",
            expected_revision=claimed.revision - 1,
            error="network",
        )

    interrupted = repository.interrupt_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=claimed.revision,
        error="Authorization: Bearer secret-token; sk-secret-value",
        phase=FailurePhase.PROVIDER,
        now=_NOW + timedelta(seconds=1),
    )
    history = repository.list_failure_history(spec.task_key)
    assert interrupted.status is RunJobStatus.INTERRUPTED
    assert len(history) == 1
    assert history[0].attempt == 1
    assert history[0].phase is FailurePhase.PROVIDER
    assert "secret-token" not in history[0].error
    assert "sk-secret-value" not in history[0].error
    assert repository.list_auto_resume_jobs("campaign", now=_NOW + timedelta(seconds=2)) == (
        interrupted,
    )


def test_running_pause_is_orthogonal_to_lease_revision(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "pause.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)

    paused = repository.request_pause(
        spec.task_key,
        expected_revision=claimed.revision,
        now=_NOW + timedelta(seconds=1),
    )
    store = CampaignCheckpointStore(
        repository,
        task_key=spec.task_key,
        lease_owner="worker",
        revision=claimed.revision,
        clock=lambda: _NOW + timedelta(seconds=2),
    )

    assert paused.pause_requested
    assert paused.revision == claimed.revision
    assert store.renew_lease(30)
    stopped = repository.stop_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=store.revision,
        reason=RunStopReason.USER_REQUESTED,
        error="user requested pause",
        now=_NOW + timedelta(seconds=3),
    )
    assert stopped.stop_reason is RunStopReason.USER_REQUESTED
    assert repository.list_failure_history(spec.task_key) == ()


def test_user_pause_wins_a_provider_failure_race(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "pause-race.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)
    repository.request_pause(
        spec.task_key,
        expected_revision=claimed.revision,
        now=_NOW + timedelta(seconds=1),
    )

    stopped = repository.interrupt_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=claimed.revision,
        error="provider transport failed",
        phase=FailurePhase.PROVIDER,
        now=_NOW + timedelta(seconds=2),
    )

    assert stopped.status is RunJobStatus.STOPPED
    assert stopped.stop_reason is RunStopReason.USER_REQUESTED
    assert repository.list_failure_history(spec.task_key) == ()


def test_resume_consumes_an_expired_unobserved_pause(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "expired-pause.sqlite3")
    spec = _prepare(repository)
    claimed = repository.claim_task(
        spec.task_key,
        lease_owner="expired-worker",
        lease_seconds=30,
        now=_NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    paused = repository.request_pause(
        spec.task_key,
        expected_revision=claimed.revision,
        now=_NOW + timedelta(seconds=2),
    )

    resumed = repository.resume_recoverable(
        spec.task_key,
        expected_revision=paused.revision,
        now=_NOW + timedelta(seconds=40),
    )

    assert resumed.status is RunJobStatus.QUEUED
    assert resumed.stop_reason is None
    assert resumed.revision == claimed.revision + 2


def test_provider_exhaustion_preserves_recovery_and_grants_fresh_attempt_block(
    tmp_path: Path,
) -> None:
    repository = CampaignRepository(tmp_path / "provider-retry.sqlite3")
    spec = _prepare(repository, attempt_limit=2)
    config = _config()
    first = _claim(repository)
    store = CampaignCheckpointStore(
        repository,
        task_key=spec.task_key,
        lease_owner="worker",
        revision=first.revision,
        clock=lambda: _NOW + timedelta(seconds=1),
    )
    checkpoint = _ready_checkpoint(config)
    store.save(checkpoint)
    window = _collecting_checkpoint(config, ()).collecting_window
    assert window is not None
    store.stage_decision(
        run_id=config.run_id,
        config_hash=config.config_hash,
        window=window,
        response=_decision("company-1"),
    )
    repository.interrupt_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=store.revision,
        error="provider unavailable",
        phase=FailurePhase.PROVIDER,
        now=_NOW + timedelta(seconds=2),
    )
    second = _claim(repository, at=_NOW + timedelta(seconds=3))
    stopped = repository.stop_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=second.revision,
        reason=RunStopReason.PROVIDER_RETRY_EXHAUSTED,
        error="provider unavailable",
        phase=FailurePhase.PROVIDER,
        now=_NOW + timedelta(seconds=4),
    )

    resumed = repository.resume_recoverable(
        spec.task_key,
        expected_revision=stopped.revision,
        now=_NOW + timedelta(seconds=5),
    )
    recovery = repository.load_recovery(spec.task_key)

    assert resumed.status is RunJobStatus.QUEUED
    assert resumed.attempt == 2
    assert resumed.attempt_limit is None
    assert recovery is not None and recovery.checkpoint == checkpoint
    assert len(recovery.decisions) == 1
    assert len(repository.list_failure_history(spec.task_key)) == 2


def test_stopped_requires_explicit_resume_and_terminal_failed_cannot_claim(
    tmp_path: Path,
) -> None:
    repository = CampaignRepository(tmp_path / "states.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)
    stopped = repository.stop_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=claimed.revision,
        reason=RunStopReason.USER_REQUESTED,
        error="user requested",
        now=_NOW + timedelta(seconds=1),
    )
    assert repository.list_auto_resume_jobs("campaign", now=_NOW + timedelta(seconds=2)) == ()
    queued = repository.resume_stopped(
        spec.task_key,
        expected_revision=stopped.revision,
        now=_NOW + timedelta(seconds=2),
    )
    claimed_again = repository.claim_next(
        "campaign", lease_owner="worker", lease_seconds=30, now=_NOW + timedelta(seconds=3)
    )
    assert claimed_again is not None
    assert claimed_again.attempt == 2
    failed = repository.fail_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=claimed_again.revision,
        error="bad configuration",
        phase=FailurePhase.CONFIGURATION,
        now=_NOW + timedelta(seconds=4),
    )
    assert queued.status is RunJobStatus.QUEUED
    assert failed.status is RunJobStatus.FAILED
    assert (
        repository.claim_next(
            "campaign", lease_owner="other", lease_seconds=30, now=_NOW + timedelta(seconds=5)
        )
        is None
    )


def test_completion_is_atomic_terminal_and_gates_campaign_publication(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "completion.sqlite3")
    spec = _prepare(repository)
    claimed = _claim(repository)
    config = _config()
    incomplete = _ready_checkpoint(config)
    result, receipt, complete = _completion_artifacts(config, spec.lineup_id, spec.seed)

    with pytest.raises(InvalidTransitionError, match="complete checkpoint"):
        repository.complete_run(
            spec.task_key,
            lease_owner="worker",
            expected_revision=claimed.revision,
            checkpoint=incomplete,
            result=result,
            receipt=receipt,
            now=_NOW + timedelta(seconds=1),
        )
    with pytest.raises(InvalidTransitionError, match="every Run"):
        repository.complete_campaign("campaign", analysis=_empty_analysis())

    terminal = repository.save_checkpoint(
        spec.task_key,
        lease_owner="worker",
        expected_revision=claimed.revision,
        checkpoint=complete,
        now=_NOW + timedelta(seconds=2),
    )
    recovery = repository.load_recovery(spec.task_key)
    assert recovery is not None
    assert recovery.checkpoint == complete

    with pytest.raises(RepositoryConflictError, match="replay receipt"):
        repository.complete_run(
            spec.task_key,
            lease_owner="worker",
            expected_revision=terminal.revision,
            checkpoint=complete,
            result=result,
            receipt=receipt.model_copy(update={"checkpoint_sha256": "0" * 64}),
            now=_NOW + timedelta(seconds=3),
        )

    completed = repository.complete_run(
        spec.task_key,
        lease_owner="worker",
        expected_revision=terminal.revision,
        checkpoint=complete,
        result=result,
        receipt=receipt,
        now=_NOW + timedelta(seconds=4),
    )
    assert completed.status is RunJobStatus.COMPLETED
    recovery = repository.load_recovery(spec.task_key)
    assert recovery is not None
    assert recovery.checkpoint == complete
    assert recovery.decisions == ()
    assert repository.list_results("campaign") == (result,)
    assert repository.list_replay_receipts("campaign") == (receipt,)

    campaign = repository.complete_campaign(
        "campaign",
        analysis=_empty_analysis(),
        now=_NOW + timedelta(seconds=5),
    )
    assert campaign.status is CampaignStatus.COMPLETED
    assert repository.get_analysis("campaign") == _empty_analysis()
    assert (
        repository.complete_run(
            spec.task_key,
            lease_owner="any-owner",
            expected_revision=0,
            checkpoint=complete,
            result=result,
            receipt=receipt,
        )
        == completed
    )
    with sqlite3.connect(tmp_path / "completion.sqlite3") as connection:
        connection.execute(
            "UPDATE campaign_analyses SET payload_hash = ? WHERE campaign_id = ?",
            ("0" * 64, "campaign"),
        )
        connection.execute(
            "UPDATE run_results SET payload_hash = ? WHERE task_key = ?",
            ("0" * 64, spec.task_key),
        )
    with pytest.raises(RepositoryConflictError, match="analysis hash"):
        repository.get_analysis("campaign")
    with pytest.raises(RepositoryConflictError, match="result hash"):
        repository.list_results("campaign")


def test_run_payload_rejects_api_keys_before_database_write(tmp_path: Path) -> None:
    repository = CampaignRepository(tmp_path / "secrets.sqlite3")
    repository.create_campaign(
        campaign_id="campaign",
        spec_hash=_HASH_A,
        plan_hash=_HASH_B,
        expected_run_count=1,
    )
    with pytest.raises(ValidationError, match="credentials"):
        RunJobSpec.create(
            campaign_id="campaign",
            run_id="run",
            lineup_id="lineup",
            seed=7,
            config_hash=_HASH_A,
            run_payload_json='{"api_key":"must-not-persist"}',
        )
    with sqlite3.connect(tmp_path / "secrets.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_jobs").fetchone()[0] == 0


def _prepare(
    repository: CampaignRepository,
    *,
    attempt_limit: int | None = DEFAULT_MAX_RUN_ATTEMPTS,
) -> RunJobSpec:
    config = _config()
    run = _campaign_run()
    repository.create_campaign(
        campaign_id="campaign",
        spec_hash=_HASH_A,
        plan_hash=_HASH_B,
        expected_run_count=1,
        now=_NOW,
    )
    spec = RunJobSpec.create(
        campaign_id="campaign",
        run_id=config.run_id,
        lineup_id=run.lineup_id,
        seed=config.scenario.seed,
        config_hash=config.config_hash,
        run_payload_json=run.model_dump_json(),
    )
    assert (
        repository.enqueue_runs(
            (spec,),
            attempt_limit=attempt_limit,
            now=_NOW,
        )[0].task_key
        == spec.task_key
    )
    return spec


def _claim(
    repository: CampaignRepository,
    *,
    owner: str = "worker",
    at: datetime = _NOW,
    lease_seconds: float = 30,
):
    claimed = repository.claim_next(
        "campaign", lease_owner=owner, lease_seconds=lease_seconds, now=at
    )
    assert claimed is not None
    return claimed


def _config() -> RunConfig:
    scenario = default_scenario(total_days=14).model_copy(update={"seed": 7})
    run = _campaign_run()
    assignments = tuple(
        AgentAssignment(
            company_id=company_id,
            model_id=model_id,
            harness_id="opencode-native-test",
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
        scenario=scenario,
        assignments=assignments,
        provider_endpoint_sha256="0" * 64,
    )


def _campaign_run() -> CampaignRun:
    plan = CampaignCompiler().compile(
        ExperimentSpec(
            campaign_id="campaign",
            base_url="https://newapi.example/v1",
            company_count=2,
            days=14,
            seeds=(7,),
            api=(ApiSpec(api_key="temporary-test-key", models=("model-1", "model-2")),),
        )
    )
    return next(run for run in plan.runs if run.model_ids == ("model-1", "model-2"))


def _ready_checkpoint(config: RunConfig) -> RunCheckpoint:
    initial = MarketWorld(config.scenario).create_initial_state(config.company_ids)
    return RunCheckpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
        phase=CheckpointPhase.READY,
        initial_state=initial,
        current_state=initial,
        next_window_index=0,
    )


def _collecting_checkpoint(
    config: RunConfig,
    decisions: tuple[SealedDecision, ...],
) -> RunCheckpoint:
    checkpoint = _ready_checkpoint(config)
    collecting = CollectingWindow(
        window_index=0,
        start_day=0,
        start_world_version=0,
        start_state_hash=canonical_model_hash(checkpoint.current_state),
        expected_company_ids=config.company_ids,
        decisions=decisions,
    )
    return checkpoint.model_copy(
        update={
            "phase": CheckpointPhase.COLLECTING_DECISIONS,
            "collecting_window": collecting,
        }
    )


def _sealed_checkpoint(collecting_checkpoint: RunCheckpoint) -> RunCheckpoint:
    collecting = collecting_checkpoint.collecting_window
    assert collecting is not None
    pending = PendingWindow(
        window_index=collecting.window_index,
        start_day=collecting.start_day,
        start_world_version=collecting.start_world_version,
        start_state_hash=collecting.start_state_hash,
        decisions=collecting.decisions,
    )
    return collecting_checkpoint.model_copy(
        update={
            "phase": CheckpointPhase.DECISIONS_SEALED,
            "collecting_window": None,
            "pending_window": pending,
        }
    )


def _committed_checkpoint(config: RunConfig, sealed_checkpoint: RunCheckpoint) -> RunCheckpoint:
    pending = sealed_checkpoint.pending_window
    assert pending is not None
    world = MarketWorld(config.scenario)
    result = world.advance_window(
        sealed_checkpoint.current_state,
        {item.company_id: item.decision for item in pending.decisions},
    )
    completed = CompletedWindow(
        **pending.model_dump(),
        end_day=result.end_day,
        end_world_version=result.world_version,
        day_fingerprints=tuple(
            DayFingerprint(
                day=day.day,
                world_version=day.world_version,
                state_hash=day.state_hash,
            )
            for day in result.days
        ),
        end_state_hash=canonical_model_hash(result.state),
    )
    return RunCheckpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
        phase=CheckpointPhase.READY,
        initial_state=sealed_checkpoint.initial_state,
        current_state=result.state,
        next_window_index=1,
        completed_windows=(completed,),
    )


def _decision(company_id: str) -> SealedDecision:
    return SealedDecision(
        company_id=company_id,
        agent_status="sealed",
        decision=CompanyDecision(company_id=company_id, world_version=0),
        usage=AgentUsage(),
    )


def _completion_artifacts(
    config: RunConfig, lineup_id: str, seed: int
) -> tuple[ArenaRunResult, ReplayReceipt, RunCheckpoint]:
    initial = MarketWorld(config.scenario).create_initial_state(config.company_ids)
    bankrupt = initial.model_copy(
        update={
            "companies": {
                company_id: company.model_copy(update={"status": CompanyStatus.BANKRUPT})
                for company_id, company in initial.companies.items()
            }
        },
        deep=True,
    )
    checkpoint = RunCheckpoint(
        run_id=config.run_id,
        config_hash=config.config_hash,
        phase=CheckpointPhase.COMPLETE,
        initial_state=bankrupt,
        current_state=bankrupt,
        next_window_index=0,
    )
    result = ArenaRunResult(
        run_id=config.run_id,
        lineup_id=lineup_id,
        seed=seed,
        assignments=config.assignments,
        official_score_by_company_cents={company_id: 0 for company_id in config.company_ids},
        decision_status_by_company={company_id: {"sealed": 1} for company_id in config.company_ids},
        scenario_id=config.scenario.scenario_id,
        config_hash=config.config_hash,
    )
    receipt = ReplayReceipt(
        run_id=config.run_id,
        config_hash=config.config_hash,
        checkpoint_sha256=canonical_model_hash(checkpoint),
        runtime_trace_sha256="d" * 64,
        verified_windows=0,
        verified_days=0,
        final_state_hash=canonical_model_hash(bankrupt),
        verifier_fingerprints={},
    )
    return result, receipt, checkpoint


def _empty_analysis() -> ArenaAnalysis:
    return ArenaAnalysis(track_a={}, track_b={}, track_c={}, health={})
