"""Durable Campaign lifecycle, lease, and recovery storage."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ceo_arena.campaign.models import (
    DEFAULT_MAX_RUN_ATTEMPTS,
)
from ceo_arena.campaign.records import (
    CampaignRecord,
    CampaignStatus,
    DecisionResponseRecord,
    DecisionResponseStatus,
    FailureHistoryRecord,
    FailurePhase,
    RunJobRecord,
    RunJobSpec,
    RunJobStatus,
    RunRecoveryRecord,
    RunStopReason,
    campaign_decision_id,
    require_aware_timestamp,
)
from ceo_arena.campaign.storage import SQLiteStorage
from ceo_arena.evaluation import ArenaAnalysis, ArenaRunResult
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import CompanyStatus
from ceo_arena.runtime._redact import redact_sensitive_text
from ceo_arena.runtime.persistence import (
    CheckpointPhase,
    CollectingWindow,
    RunCheckpoint,
    SealedDecision,
)
from ceo_arena.runtime.replay import ReplayReceipt

CAMPAIGN_DATABASE_SCHEMA_VERSION = 4


class CampaignRepositoryError(RuntimeError):
    """Base class for durable Campaign contract violations."""


class RepositoryConflictError(CampaignRepositoryError):
    """Raised when an immutable identity is reused with different content."""


class RevisionConflictError(CampaignRepositoryError):
    """Raised when a caller attempts a stale compare-and-swap mutation."""


class LeaseLostError(CampaignRepositoryError):
    """Raised when a worker no longer owns a live Run lease."""


class InvalidTransitionError(CampaignRepositoryError):
    """Raised when a lifecycle transition violates the Run state machine."""


class PauseRequestedError(CampaignRepositoryError):
    """Raised when completion races with an accepted pause request."""

    def __init__(self, revision: int) -> None:
        super().__init__("Run pause was requested before completion")
        self.revision = revision


class CampaignRepository:
    """Own Campaign lifecycle, leases, recovery frontiers, and publication gates."""

    def __init__(self, database: str | Path, *, busy_timeout_ms: int = 30_000) -> None:
        self._storage = SQLiteStorage(database, busy_timeout_ms=busy_timeout_ms)
        self._storage.initialize(
            _SCHEMA,
            schema_version=CAMPAIGN_DATABASE_SCHEMA_VERSION,
        )

    def create_campaign(
        self,
        *,
        campaign_id: str,
        spec_hash: str,
        plan_hash: str,
        expected_run_count: int,
        now: datetime | None = None,
    ) -> CampaignRecord:
        """Create one immutable Campaign or validate an exact retry."""

        timestamp = _timestamp(now)
        expected = CampaignRecord(
            campaign_id=campaign_id,
            spec_hash=spec_hash,
            plan_hash=plan_hash,
            expected_run_count=expected_run_count,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO campaigns (
                    campaign_id, spec_hash, plan_hash, expected_run_count,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO NOTHING
                """,
                (
                    expected.campaign_id,
                    expected.spec_hash,
                    expected.plan_hash,
                    expected.expected_run_count,
                    expected.status.value,
                    _dump_time(expected.created_at),
                    _dump_time(expected.updated_at),
                ),
            )
            actual = self._campaign_from_row(_require_row(connection, campaign_id))
            if (
                actual.spec_hash,
                actual.plan_hash,
                actual.expected_run_count,
            ) != (expected.spec_hash, expected.plan_hash, expected.expected_run_count):
                raise RepositoryConflictError("Campaign identity already has different content")
            return actual

    def get_campaign(self, campaign_id: str) -> CampaignRecord | None:
        """Return one Campaign when present."""

        with self._storage.connect() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        return self._campaign_from_row(row) if row is not None else None

    def enqueue_runs(
        self,
        specs: tuple[RunJobSpec, ...],
        *,
        attempt_limit: int | None = None,
        now: datetime | None = None,
    ) -> tuple[RunJobRecord, ...]:
        """Atomically add immutable Run tasks and make exact retries idempotent."""

        if attempt_limit is not None and attempt_limit < 1:
            raise ValueError("attempt_limit must be positive")
        if not specs:
            return ()
        campaign_ids = {spec.campaign_id for spec in specs}
        if len(campaign_ids) != 1:
            raise ValueError("one enqueue operation must target one Campaign")
        if len({spec.task_key for spec in specs}) != len(specs):
            raise ValueError("enqueue operation contains duplicate task keys")
        campaign_id = specs[0].campaign_id
        timestamp = _timestamp(now)
        with self._storage.transaction() as connection:
            campaign = self._campaign_from_row(_require_row(connection, campaign_id))
            if campaign.status is CampaignStatus.COMPLETED:
                raise InvalidTransitionError("completed Campaign cannot accept new Runs")
            existing_count = connection.execute(
                "SELECT COUNT(*) FROM run_jobs WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()[0]
            new_count = sum(
                connection.execute(
                    "SELECT 1 FROM run_jobs WHERE task_key = ?", (spec.task_key,)
                ).fetchone()
                is None
                for spec in specs
            )
            if existing_count + new_count > campaign.expected_run_count:
                raise InvalidTransitionError("Run tasks exceed the frozen Campaign plan")
            records: list[RunJobRecord] = []
            for spec in specs:
                connection.execute(
                    """
                    INSERT INTO run_jobs (
                        task_key, campaign_id, run_id, lineup_id, seed, config_hash,
                        run_payload_json, status, revision, current_day, attempt, attempt_limit,
                        pause_requested, stop_reason, retry_not_before,
                        lease_owner, lease_until, last_error, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?,
                        0, NULL, NULL, NULL, NULL, NULL, ?, ?
                    )
                    ON CONFLICT(task_key) DO NOTHING
                    """,
                    (
                        spec.task_key,
                        spec.campaign_id,
                        spec.run_id,
                        spec.lineup_id,
                        spec.seed,
                        spec.config_hash,
                        spec.run_payload_json,
                        RunJobStatus.QUEUED.value,
                        attempt_limit,
                        _dump_time(timestamp),
                        _dump_time(timestamp),
                    ),
                )
                row = _require_job_row(connection, spec.task_key)
                actual = self._job_from_row(row)
                if _job_spec(actual) != spec:
                    raise RepositoryConflictError("Run task identity already has different content")
                if actual.attempt_limit is not None and (
                    attempt_limit is None or actual.attempt_limit < attempt_limit
                ):
                    self._cas_update(
                        connection,
                        actual.task_key,
                        actual.revision,
                        "attempt_limit = ?, updated_at = ?",
                        (attempt_limit, _dump_time(timestamp)),
                    )
                    actual = self._job_from_row(_require_job_row(connection, spec.task_key))
                records.append(actual)
            return tuple(records)

    def get_job(self, task_key: str) -> RunJobRecord | None:
        """Return one Run lifecycle record when present."""

        with self._storage.connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_jobs WHERE task_key = ?", (task_key,)
            ).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, campaign_id: str) -> tuple[RunJobRecord, ...]:
        """Return Campaign tasks in stable lineup and Seed order."""

        with self._storage.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_jobs
                WHERE campaign_id = ?
                ORDER BY seed ASC, lineup_id ASC, task_key ASC
                """,
                (campaign_id,),
            ).fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    def list_auto_resume_jobs(
        self, campaign_id: str, *, now: datetime | None = None
    ) -> tuple[RunJobRecord, ...]:
        """List queued/interrupted Runs and running Runs whose leases expired."""

        timestamp = _dump_time(_timestamp(now))
        with self._storage.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_jobs
                WHERE campaign_id = ? AND (
                    status = ?
                    OR (status = ? AND (retry_not_before IS NULL OR retry_not_before <= ?))
                    OR (status = ? AND lease_until <= ? AND pause_requested = 0)
                )
                ORDER BY created_at ASC, task_key ASC
                """,
                (
                    campaign_id,
                    RunJobStatus.QUEUED.value,
                    RunJobStatus.INTERRUPTED.value,
                    timestamp,
                    RunJobStatus.RUNNING.value,
                    timestamp,
                ),
            ).fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    def claim_next(
        self,
        campaign_id: str,
        *,
        lease_owner: str,
        lease_seconds: float,
        seed: int | None = None,
        exclude_task_keys: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> RunJobRecord | None:
        """Claim exactly one resumable Run under a revision-checked lease."""

        if not lease_owner:
            raise ValueError("lease_owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = _timestamp(now)
        excluded = (
            f"AND task_key NOT IN ({','.join('?' for _ in exclude_task_keys)})"
            if exclude_task_keys
            else ""
        )
        with self._storage.transaction() as connection:
            self._stop_expired_pauses(
                connection,
                campaign_id=campaign_id,
                task_key=None,
                now=claimed_at,
            )
            row = connection.execute(
                f"""
                SELECT * FROM run_jobs
                WHERE campaign_id = ? AND (? IS NULL OR seed = ?) {excluded} AND (
                    status = ?
                    OR (status = ? AND (retry_not_before IS NULL OR retry_not_before <= ?))
                    OR (status = ? AND lease_until <= ? AND pause_requested = 0)
                )
                ORDER BY
                    CASE status WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END,
                    created_at ASC,
                    task_key ASC
                LIMIT 1
                """,
                (
                    campaign_id,
                    seed,
                    seed,
                    *exclude_task_keys,
                    RunJobStatus.QUEUED.value,
                    RunJobStatus.INTERRUPTED.value,
                    _dump_time(claimed_at),
                    RunJobStatus.RUNNING.value,
                    _dump_time(claimed_at),
                    RunJobStatus.INTERRUPTED.value,
                    RunJobStatus.QUEUED.value,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._claim_selected(
                connection,
                row,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                claimed_at=claimed_at,
            )

    def claim_task(
        self,
        task_key: str,
        *,
        lease_owner: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> RunJobRecord | None:
        """Claim one exact resumable task, primarily for verified migration."""

        if not lease_owner:
            raise ValueError("lease_owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = _timestamp(now)
        with self._storage.transaction() as connection:
            self._stop_expired_pauses(
                connection,
                campaign_id=None,
                task_key=task_key,
                now=claimed_at,
            )
            row = connection.execute(
                """
                SELECT * FROM run_jobs WHERE task_key = ? AND (
                    status = ?
                    OR (status = ? AND (retry_not_before IS NULL OR retry_not_before <= ?))
                    OR (status = ? AND lease_until <= ? AND pause_requested = 0)
                )
                """,
                (
                    task_key,
                    RunJobStatus.QUEUED.value,
                    RunJobStatus.INTERRUPTED.value,
                    _dump_time(claimed_at),
                    RunJobStatus.RUNNING.value,
                    _dump_time(claimed_at),
                ),
            ).fetchone()
            if row is None:
                return None
            return self._claim_selected(
                connection,
                row,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                claimed_at=claimed_at,
            )

    def _claim_selected(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        lease_owner: str,
        lease_seconds: float,
        claimed_at: datetime,
    ) -> RunJobRecord:
        """CAS one selected eligible row and update its Campaign lifecycle."""

        changed = connection.execute(
            """
            UPDATE run_jobs
            SET status = ?, revision = revision + 1, attempt = attempt + 1,
                pause_requested = 0, stop_reason = NULL, retry_not_before = NULL,
                lease_owner = ?, lease_until = ?, last_error = NULL, updated_at = ?
            WHERE task_key = ? AND revision = ? AND (
                status = ?
                OR (status = ? AND (retry_not_before IS NULL OR retry_not_before <= ?))
                OR (status = ? AND lease_until <= ? AND pause_requested = 0)
            )
            """,
            (
                RunJobStatus.RUNNING.value,
                lease_owner,
                _dump_time(claimed_at + timedelta(seconds=lease_seconds)),
                _dump_time(claimed_at),
                row["task_key"],
                row["revision"],
                RunJobStatus.QUEUED.value,
                RunJobStatus.INTERRUPTED.value,
                _dump_time(claimed_at),
                RunJobStatus.RUNNING.value,
                _dump_time(claimed_at),
            ),
        ).rowcount
        if changed != 1:
            raise RevisionConflictError("Run claim lost its compare-and-swap race")
        connection.execute(
            """
            UPDATE campaigns SET status = ?, updated_at = ?
            WHERE campaign_id = ? AND status != ?
            """,
            (
                CampaignStatus.RUNNING.value,
                _dump_time(claimed_at),
                row["campaign_id"],
                CampaignStatus.COMPLETED.value,
            ),
        )
        return self._job_from_row(_require_job_row(connection, row["task_key"]))

    def renew_lease(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Extend one live lease using revision compare-and-swap."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        renewed_at = _timestamp(now)
        with self._storage.transaction() as connection:
            job = self._require_live_lease(
                connection, task_key, lease_owner, expected_revision, renewed_at
            )
            if job.pause_requested:
                return job
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                "lease_until = ?, updated_at = ?",
                (_dump_time(renewed_at + timedelta(seconds=lease_seconds)), _dump_time(renewed_at)),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    def request_pause(
        self,
        task_key: str,
        *,
        expected_revision: int,
        message: str = "user requested pause",
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Request a leased pause or immediately stop an unleased resumable Run."""

        requested_at = _timestamp(now)
        clean_message = _clean_error(message)
        with self._storage.transaction() as connection:
            row = _require_job_row(connection, task_key)
            if row["revision"] != expected_revision:
                raise RevisionConflictError("Run revision changed")
            job = self._job_from_row(row)
            if job.status is RunJobStatus.RUNNING:
                if job.pause_requested:
                    return job
                changed = connection.execute(
                    """
                    UPDATE run_jobs SET pause_requested = 1, updated_at = ?
                    WHERE task_key = ? AND revision = ? AND status = ?
                    """,
                    (
                        _dump_time(requested_at),
                        task_key,
                        expected_revision,
                        RunJobStatus.RUNNING.value,
                    ),
                ).rowcount
                if changed != 1:
                    raise RevisionConflictError("pause request lost its compare-and-swap race")
                return self._job_from_row(_require_job_row(connection, task_key))
            if job.status in {RunJobStatus.QUEUED, RunJobStatus.INTERRUPTED}:
                self._cas_update(
                    connection,
                    task_key,
                    expected_revision,
                    """
                    status = ?, pause_requested = 0, stop_reason = ?,
                    retry_not_before = NULL, last_error = ?, updated_at = ?
                    """,
                    (
                        RunJobStatus.STOPPED.value,
                        RunStopReason.USER_REQUESTED.value,
                        clean_message,
                        _dump_time(requested_at),
                    ),
                )
                return self._job_from_row(_require_job_row(connection, task_key))
            if (
                job.status is RunJobStatus.STOPPED
                and job.stop_reason is RunStopReason.USER_REQUESTED
            ):
                return job
            raise InvalidTransitionError("only a resumable Run can be paused")

    def stage_decision(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        window_index: int,
        response: SealedDecision,
        window: CollectingWindow | None = None,
        run_id: str | None = None,
        config_hash: str | None = None,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Atomically stage one normalized decision before economy mutation."""

        if window_index < 0:
            raise ValueError("window_index must be non-negative")
        staged_at = _timestamp(now)
        with self._storage.transaction() as connection:
            job = self._require_live_lease(
                connection, task_key, lease_owner, expected_revision, staged_at
            )
            if response.company_id not in job.run_payload.company_ids:
                raise RepositoryConflictError("decision company is not assigned to this Run")
            if window is not None:
                saved = connection.execute(
                    "SELECT frontier, current_day, world_version, state_hash "
                    "FROM checkpoints WHERE task_key = ?",
                    (task_key,),
                ).fetchone()
                if (
                    saved is None
                    or job.run_id != run_id
                    or job.config_hash != config_hash
                    or window_index != window.window_index
                    or response.company_id not in window.expected_company_ids
                    or response.decision.world_version != window.start_world_version
                    or (
                        saved["frontier"],
                        saved["current_day"],
                        saved["world_version"],
                        saved["state_hash"],
                    )
                    != (
                        window.window_index,
                        window.start_day,
                        window.start_world_version,
                        window.start_state_hash,
                    )
                ):
                    raise RepositoryConflictError(
                        "decision receipt differs from the frozen checkpoint frontier"
                    )
            inserted = self._stage_response_row(
                connection,
                task_key=task_key,
                window_index=window_index,
                response=response,
                staged_at=staged_at,
            )
            if not inserted:
                return self._job_from_row(_require_job_row(connection, task_key))
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                "updated_at = ?",
                (_dump_time(staged_at),),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    def save_checkpoint(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        checkpoint: RunCheckpoint,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Atomically save any runtime phase, staged responses, and committed frontier."""

        committed_at = _timestamp(now)
        with self._storage.transaction() as connection:
            job = self._require_live_lease(
                connection, task_key, lease_owner, expected_revision, committed_at
            )
            self._validate_checkpoint(job, checkpoint)
            existing = connection.execute(
                "SELECT * FROM checkpoints WHERE task_key = ?",
                (task_key,),
            ).fetchone()
            checkpoint_json = checkpoint.model_dump_json()
            previous = (
                RunCheckpoint.model_validate_json(existing["checkpoint_json"])
                if existing is not None
                else None
            )
            self._validate_checkpoint_transition(previous, checkpoint)
            if checkpoint.phase is CheckpointPhase.COMPLETE:
                self._validate_terminal_checkpoint(job, checkpoint)
            window = checkpoint.collecting_window or checkpoint.pending_window
            if window is not None and window.decisions:
                stored = {
                    row["company_id"]: row["response_hash"]
                    for row in connection.execute(
                        "SELECT company_id, response_hash FROM decision_responses "
                        "WHERE task_key = ? AND window_index = ?",
                        (task_key, checkpoint.next_window_index),
                    )
                }
                for response in window.decisions:
                    if response.company_id in stored:
                        if stored[response.company_id] != canonical_model_hash(response):
                            raise RepositoryConflictError(
                                "snapshot has different content from its durable decision receipt"
                            )
                    else:
                        self._stage_response_row(
                            connection,
                            task_key=task_key,
                            window_index=checkpoint.next_window_index,
                            response=response,
                            staged_at=committed_at,
                        )
            connection.execute(
                """
                INSERT INTO checkpoints (
                    task_key, frontier, current_day, checkpoint_hash,
                    checkpoint_json, updated_at, world_version, state_hash, phase, active_companies
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    frontier = excluded.frontier,
                    current_day = excluded.current_day,
                    checkpoint_hash = excluded.checkpoint_hash,
                    checkpoint_json = excluded.checkpoint_json,
                    updated_at = excluded.updated_at,
                    world_version = excluded.world_version,
                    state_hash = excluded.state_hash,
                    phase = excluded.phase,
                    active_companies = excluded.active_companies
                """,
                (
                    task_key,
                    checkpoint.next_window_index,
                    checkpoint.current_state.day,
                    canonical_model_hash(checkpoint),
                    checkpoint_json,
                    _dump_time(committed_at),
                    checkpoint.current_state.version,
                    canonical_model_hash(checkpoint.current_state),
                    checkpoint.phase.value,
                    sum(
                        company.status is CompanyStatus.ACTIVE
                        for company in checkpoint.current_state.companies.values()
                    ),
                ),
            )
            if checkpoint.next_window_index:
                connection.execute(
                    """
                    UPDATE decision_responses
                    SET status = ?, applied_at = COALESCE(applied_at, ?)
                    WHERE task_key = ? AND window_index < ?
                    """,
                    (
                        DecisionResponseStatus.APPLIED.value,
                        _dump_time(committed_at),
                        task_key,
                        checkpoint.next_window_index,
                    ),
                )
            unapplied = connection.execute(
                """
                SELECT COUNT(*) FROM decision_responses
                WHERE task_key = ? AND window_index < ? AND status = ?
                """,
                (
                    task_key,
                    checkpoint.next_window_index,
                    DecisionResponseStatus.SEALED.value,
                ),
            ).fetchone()[0]
            if unapplied:
                raise RepositoryConflictError("checkpoint advances past an unapplied decision")
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                "current_day = ?, updated_at = ?",
                (checkpoint.current_state.day, _dump_time(committed_at)),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    def load_recovery(self, task_key: str) -> RunRecoveryRecord | None:
        """Load one Run with its authoritative checkpoint and staged responses."""

        with self._storage.connect() as connection:
            job_row = connection.execute(
                "SELECT * FROM run_jobs WHERE task_key = ?", (task_key,)
            ).fetchone()
            if job_row is None:
                return None
            checkpoint_row = connection.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE task_key = ?", (task_key,)
            ).fetchone()
            decision_rows = connection.execute(
                """
                SELECT * FROM decision_responses
                WHERE task_key = ?
                ORDER BY window_index ASC, company_id ASC
                """,
                (task_key,),
            ).fetchall()
        return RunRecoveryRecord(
            job=self._job_from_row(job_row),
            checkpoint=(
                RunCheckpoint.model_validate_json(checkpoint_row["checkpoint_json"])
                if checkpoint_row is not None
                else None
            ),
            decisions=tuple(self._decision_from_row(row) for row in decision_rows),
        )

    def interrupt_run(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        error: str,
        phase: FailurePhase = FailurePhase.RUNTIME,
        retry_delay_seconds: float = 0,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Release a recoverable failed attempt for automatic resume."""

        if retry_delay_seconds < 0 or retry_delay_seconds > 300:
            raise ValueError("retry delay must be between zero and 300 seconds")
        return self._finish_attempt(
            task_key,
            lease_owner=lease_owner,
            expected_revision=expected_revision,
            status=RunJobStatus.INTERRUPTED,
            error=error,
            phase=phase,
            stop_reason=None,
            retry_delay_seconds=retry_delay_seconds,
            now=now,
        )

    def stop_run(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        reason: RunStopReason,
        error: str,
        phase: FailurePhase | None = None,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Release a Run that requires an explicit resume."""

        return self._finish_attempt(
            task_key,
            lease_owner=lease_owner,
            expected_revision=expected_revision,
            status=RunJobStatus.STOPPED,
            error=error,
            phase=phase,
            stop_reason=reason,
            retry_delay_seconds=0,
            now=now,
        )

    def fail_run(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        error: str,
        phase: FailurePhase,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Release a permanently failed Run and make it terminal."""

        return self._finish_attempt(
            task_key,
            lease_owner=lease_owner,
            expected_revision=expected_revision,
            status=RunJobStatus.FAILED,
            error=error,
            phase=phase,
            stop_reason=None,
            retry_delay_seconds=0,
            now=now,
        )

    def resume_recoverable(
        self,
        task_key: str,
        *,
        expected_revision: int,
        minimum_attempt_limit: int | None = DEFAULT_MAX_RUN_ATTEMPTS,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Requeue a stopped Run or a checkpoint-valid legacy provider failure."""

        if minimum_attempt_limit is not None and minimum_attempt_limit < 1:
            raise ValueError("minimum_attempt_limit must be positive")
        resumed_at = _timestamp(now)
        with self._storage.transaction() as connection:
            pause_finalized = self._stop_expired_pauses(
                connection,
                campaign_id=None,
                task_key=task_key,
                now=resumed_at,
            )
            if pause_finalized:
                expected_revision += 1
            row = _require_job_row(connection, task_key)
            if row["revision"] != expected_revision:
                raise RevisionConflictError("Run revision changed")
            job = self._job_from_row(row)
            if job.status is RunJobStatus.QUEUED:
                return job
            if (
                job.status is RunJobStatus.RUNNING
                and job.lease_until is not None
                and job.lease_until <= resumed_at
            ):
                return job
            provider_recovery = job.status is RunJobStatus.FAILED or (
                job.status is RunJobStatus.STOPPED
                and job.stop_reason is RunStopReason.PROVIDER_RETRY_EXHAUSTED
            )
            if provider_recovery:
                self._validate_provider_recovery(connection, job)
            elif not (
                job.status is RunJobStatus.STOPPED
                and job.stop_reason is RunStopReason.USER_REQUESTED
            ):
                raise InvalidTransitionError("Run is not explicitly recoverable")
            if job.attempt_limit is None or minimum_attempt_limit is None:
                attempt_limit = None
            elif provider_recovery:
                attempt_limit = max(job.attempt_limit, minimum_attempt_limit)
                if job.attempt >= job.attempt_limit:
                    attempt_limit = job.attempt + minimum_attempt_limit
            else:
                attempt_limit = max(job.attempt_limit, job.attempt + 1)
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                """
                status = ?, attempt_limit = ?, pause_requested = 0,
                stop_reason = NULL, retry_not_before = NULL,
                last_error = NULL, updated_at = ?
                """,
                (
                    RunJobStatus.QUEUED.value,
                    attempt_limit,
                    _dump_time(resumed_at),
                ),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    def resume_stopped(
        self,
        task_key: str,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Compatibility wrapper for the typed recoverable-resume transition."""

        return self.resume_recoverable(
            task_key,
            expected_revision=expected_revision,
            now=now,
        )

    def release_for_resume(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Release a verified imported frontier without recording a false failure."""

        released_at = _timestamp(now)
        with self._storage.transaction() as connection:
            self._require_live_lease(
                connection,
                task_key,
                lease_owner,
                expected_revision,
                released_at,
            )
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                """
                status = ?, lease_owner = NULL, lease_until = NULL,
                pause_requested = 0, stop_reason = NULL, retry_not_before = NULL,
                last_error = NULL, updated_at = ?
                """,
                (RunJobStatus.INTERRUPTED.value, _dump_time(released_at)),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    def complete_run(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        checkpoint: RunCheckpoint,
        result: ArenaRunResult,
        receipt: ReplayReceipt,
        now: datetime | None = None,
    ) -> RunJobRecord:
        """Atomically publish verified artifacts and make the Run terminal."""

        completed_at = _timestamp(now)
        result_json = result.model_dump_json()
        receipt_json = receipt.model_dump_json()
        with self._storage.transaction() as connection:
            row = _require_job_row(connection, task_key)
            if row["status"] == RunJobStatus.COMPLETED.value:
                self._require_same_artifact(connection, "run_results", task_key, result_json)
                self._require_same_artifact(connection, "replay_receipts", task_key, receipt_json)
                return self._job_from_row(row)
            job = self._require_live_lease(
                connection, task_key, lease_owner, expected_revision, completed_at
            )
            if job.pause_requested:
                raise PauseRequestedError(job.revision)
            self._validate_completion(job, checkpoint, result, receipt)
            checkpoint_row = connection.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE task_key = ?",
                (task_key,),
            ).fetchone()
            if (
                checkpoint_row is None
                or checkpoint_row["checkpoint_json"] != checkpoint.model_dump_json()
            ):
                raise InvalidTransitionError(
                    "Run completion requires its terminal checkpoint to be durable"
                )
            staged_count = connection.execute(
                """
                SELECT COUNT(*) FROM decision_responses
                WHERE task_key = ? AND status = ?
                """,
                (task_key, DecisionResponseStatus.SEALED.value),
            ).fetchone()[0]
            if staged_count:
                raise InvalidTransitionError("Run cannot complete with unapplied decisions")
            self._insert_artifact(
                connection,
                table="run_results",
                task_key=task_key,
                payload_json=result_json,
                payload_hash=canonical_model_hash(result),
                created_at=completed_at,
            )
            self._insert_artifact(
                connection,
                table="replay_receipts",
                task_key=task_key,
                payload_json=receipt_json,
                payload_hash=canonical_model_hash(receipt),
                created_at=completed_at,
            )
            connection.execute("DELETE FROM decision_responses WHERE task_key = ?", (task_key,))
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                """
                status = ?, current_day = ?, lease_owner = NULL, lease_until = NULL,
                pause_requested = 0, stop_reason = NULL, retry_not_before = NULL,
                last_error = NULL, updated_at = ?
                """,
                (
                    RunJobStatus.COMPLETED.value,
                    checkpoint.current_state.day,
                    _dump_time(completed_at),
                ),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    def complete_campaign(
        self,
        campaign_id: str,
        *,
        analysis: ArenaAnalysis,
        now: datetime | None = None,
    ) -> CampaignRecord:
        """Atomically store verified analysis and complete the full Campaign."""

        completed_at = _timestamp(now)
        analysis_json = analysis.model_dump_json()
        with self._storage.transaction() as connection:
            campaign = self._campaign_from_row(_require_row(connection, campaign_id))
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS job_count,
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS completed_count
                FROM run_jobs WHERE campaign_id = ?
                """,
                (RunJobStatus.COMPLETED.value, campaign_id),
            ).fetchone()
            result_count = connection.execute(
                """
                SELECT COUNT(*) FROM run_results AS results
                JOIN run_jobs AS jobs USING(task_key)
                WHERE jobs.campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()[0]
            receipt_count = connection.execute(
                """
                SELECT COUNT(*) FROM replay_receipts AS receipts
                JOIN run_jobs AS jobs USING(task_key)
                WHERE jobs.campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()[0]
            checkpoint_rows = connection.execute(
                """
                SELECT checkpoints.checkpoint_json FROM checkpoints
                JOIN run_jobs AS jobs USING(task_key)
                WHERE jobs.campaign_id = ?
                """,
                (campaign_id,),
            ).fetchall()
            terminal_checkpoint_count = sum(
                RunCheckpoint.model_validate_json(row["checkpoint_json"]).phase
                is CheckpointPhase.COMPLETE
                for row in checkpoint_rows
            )
            expected = campaign.expected_run_count
            if (
                counts["job_count"],
                counts["completed_count"],
                result_count,
                receipt_count,
                terminal_checkpoint_count,
            ) != (expected, expected, expected, expected, expected):
                raise InvalidTransitionError(
                    "Campaign completion requires every Run, terminal checkpoint, and receipt"
                )
            connection.execute(
                """
                INSERT INTO campaign_analyses (
                    campaign_id, plan_hash, payload_hash, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO NOTHING
                """,
                (
                    campaign_id,
                    campaign.plan_hash,
                    canonical_model_hash(analysis),
                    analysis_json,
                    _dump_time(completed_at),
                ),
            )
            stored = connection.execute(
                """
                SELECT plan_hash, payload_json FROM campaign_analyses
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if (
                stored is None
                or stored["plan_hash"] != campaign.plan_hash
                or stored["payload_json"] != analysis_json
            ):
                raise RepositoryConflictError("Campaign analysis identity conflict")
            connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE campaign_id = ?",
                (CampaignStatus.COMPLETED.value, _dump_time(completed_at), campaign_id),
            )
            return self._campaign_from_row(_require_row(connection, campaign_id))

    def mark_campaign_incomplete(
        self,
        campaign_id: str,
        *,
        now: datetime | None = None,
    ) -> CampaignRecord:
        """Mark an exhausted Campaign without misreporting it as still running."""

        updated_at = _timestamp(now)
        terminal = (
            RunJobStatus.STOPPED.value,
            RunJobStatus.FAILED.value,
            RunJobStatus.COMPLETED.value,
        )
        with self._storage.transaction() as connection:
            campaign = self._campaign_from_row(_require_row(connection, campaign_id))
            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status IN (?, ?, ?) THEN 1 ELSE 0 END) AS terminal
                FROM run_jobs WHERE campaign_id = ?
                """,
                (*terminal, campaign_id),
            ).fetchone()
            if (
                counts["total"] != campaign.expected_run_count
                or counts["terminal"] != counts["total"]
            ):
                raise InvalidTransitionError("Campaign still has resumable Runs")
            connection.execute(
                "DELETE FROM campaign_analyses WHERE campaign_id = ?",
                (campaign_id,),
            )
            connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE campaign_id = ?",
                (CampaignStatus.INCOMPLETE.value, _dump_time(updated_at), campaign_id),
            )
            return self._campaign_from_row(_require_row(connection, campaign_id))

    def get_analysis(self, campaign_id: str) -> ArenaAnalysis | None:
        """Return the transactionally published official analysis, when present."""

        with self._storage.connect() as connection:
            row = connection.execute(
                """
                SELECT analyses.plan_hash, analyses.payload_hash, analyses.payload_json,
                       campaigns.plan_hash AS campaign_plan_hash
                FROM campaign_analyses AS analyses
                JOIN campaigns USING(campaign_id)
                WHERE analyses.campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        analysis = ArenaAnalysis.model_validate_json(row["payload_json"])
        if row["plan_hash"] != row["campaign_plan_hash"] or row[
            "payload_hash"
        ] != canonical_model_hash(analysis):
            raise RepositoryConflictError("stored Campaign analysis hash is invalid")
        return analysis

    def list_results(self, campaign_id: str) -> tuple[ArenaRunResult, ...]:
        """Return verified Run results in stable lineup and Seed order."""

        artifacts = self._list_artifacts(campaign_id, "run_results")
        results = tuple(ArenaRunResult.model_validate_json(payload) for _hash, payload in artifacts)
        if any(
            stored_hash != canonical_model_hash(result)
            for (stored_hash, _payload), result in zip(artifacts, results, strict=True)
        ):
            raise RepositoryConflictError("stored Run result hash is invalid")
        return results

    def list_replay_receipts(self, campaign_id: str) -> tuple[ReplayReceipt, ...]:
        """Return replay receipts in stable lineup and Seed order."""

        artifacts = self._list_artifacts(campaign_id, "replay_receipts")
        receipts = tuple(ReplayReceipt.model_validate_json(payload) for _hash, payload in artifacts)
        if any(
            stored_hash != canonical_model_hash(receipt)
            for (stored_hash, _payload), receipt in zip(artifacts, receipts, strict=True)
        ):
            raise RepositoryConflictError("stored Replay Receipt hash is invalid")
        return receipts

    def list_failure_history(self, task_key: str) -> tuple[FailureHistoryRecord, ...]:
        """Return append-only failures for one Run attempt sequence."""

        with self._storage.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM failure_history
                WHERE task_key = ? ORDER BY sequence ASC
                """,
                (task_key,),
            ).fetchall()
        return tuple(
            FailureHistoryRecord(
                sequence=row["sequence"],
                task_key=row["task_key"],
                attempt=row["attempt"],
                phase=FailurePhase(row["phase"]),
                error=row["error"],
                occurred_at=_load_time(row["occurred_at"]),
            )
            for row in rows
        )

    def _finish_attempt(
        self,
        task_key: str,
        *,
        lease_owner: str,
        expected_revision: int,
        status: RunJobStatus,
        error: str,
        phase: FailurePhase | None,
        stop_reason: RunStopReason | None,
        retry_delay_seconds: float,
        now: datetime | None,
    ) -> RunJobRecord:
        """Release one lease and optionally append a failure in the same transaction."""

        if status not in {
            RunJobStatus.INTERRUPTED,
            RunJobStatus.STOPPED,
            RunJobStatus.FAILED,
        }:
            raise ValueError("attempt can only finish in interrupted, stopped, or failed")
        if (status is RunJobStatus.STOPPED) != (stop_reason is not None):
            raise ValueError("stopped attempts require exactly one typed reason")
        finished_at = _timestamp(now)
        clean_error = _clean_error(error)
        retry_not_before = (
            finished_at + timedelta(seconds=retry_delay_seconds)
            if status is RunJobStatus.INTERRUPTED and retry_delay_seconds
            else None
        )
        with self._storage.transaction() as connection:
            job = self._require_live_lease(
                connection, task_key, lease_owner, expected_revision, finished_at
            )
            if job.pause_requested:
                status = RunJobStatus.STOPPED
                stop_reason = RunStopReason.USER_REQUESTED
                phase = None
                retry_not_before = None
                clean_error = "user requested pause"
            if phase is not None:
                connection.execute(
                    """
                    INSERT INTO failure_history (
                        task_key, attempt, phase, error, occurred_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        task_key,
                        job.attempt,
                        phase.value,
                        clean_error,
                        _dump_time(finished_at),
                    ),
                )
            self._cas_update(
                connection,
                task_key,
                expected_revision,
                """
                status = ?, lease_owner = NULL, lease_until = NULL,
                pause_requested = 0, stop_reason = ?, retry_not_before = ?,
                last_error = ?, updated_at = ?
                """,
                (
                    status.value,
                    stop_reason.value if stop_reason is not None else None,
                    _dump_time(retry_not_before) if retry_not_before is not None else None,
                    clean_error,
                    _dump_time(finished_at),
                ),
            )
            return self._job_from_row(_require_job_row(connection, task_key))

    @staticmethod
    def _stop_expired_pauses(
        connection: sqlite3.Connection,
        *,
        campaign_id: str | None,
        task_key: str | None,
        now: datetime,
    ) -> bool:
        """Consume pause requests whose worker lease expired before observing them."""

        if (campaign_id is None) == (task_key is None):
            raise ValueError("exactly one expired-pause selector is required")
        selector = "campaign_id = ?" if campaign_id is not None else "task_key = ?"
        selected = campaign_id if campaign_id is not None else task_key
        changed = connection.execute(
            f"""
            UPDATE run_jobs
            SET status = ?, revision = revision + 1,
                pause_requested = 0, stop_reason = ?, retry_not_before = NULL,
                lease_owner = NULL, lease_until = NULL,
                last_error = ?, updated_at = ?
            WHERE {selector} AND status = ? AND pause_requested = 1 AND lease_until <= ?
            """,
            (
                RunJobStatus.STOPPED.value,
                RunStopReason.USER_REQUESTED.value,
                "user requested pause",
                _dump_time(now),
                selected,
                RunJobStatus.RUNNING.value,
                _dump_time(now),
            ),
        ).rowcount
        return changed == 1

    def _validate_provider_recovery(
        self,
        connection: sqlite3.Connection,
        job: RunJobRecord,
    ) -> None:
        """Require an intact recovery frontier for a provider-exhausted Run."""

        latest_failure = connection.execute(
            """
            SELECT phase FROM failure_history
            WHERE task_key = ? ORDER BY sequence DESC LIMIT 1
            """,
            (job.task_key,),
        ).fetchone()
        if latest_failure is None or latest_failure["phase"] != FailurePhase.PROVIDER.value:
            raise InvalidTransitionError("failed Run is not a provider recovery")
        published = connection.execute(
            """
            SELECT EXISTS(SELECT 1 FROM run_results WHERE task_key = ?)
                OR EXISTS(SELECT 1 FROM replay_receipts WHERE task_key = ?)
            """,
            (job.task_key, job.task_key),
        ).fetchone()[0]
        if published:
            raise InvalidTransitionError("published Run artifacts cannot be retried")
        row = connection.execute(
            "SELECT * FROM checkpoints WHERE task_key = ?",
            (job.task_key,),
        ).fetchone()
        if row is None:
            raise InvalidTransitionError("provider recovery requires a checkpoint")
        try:
            checkpoint = RunCheckpoint.model_validate_json(row["checkpoint_json"])
        except ValueError as error:
            raise RepositoryConflictError("provider recovery checkpoint is invalid") from error
        if (
            row["checkpoint_hash"] != canonical_model_hash(checkpoint)
            or row["frontier"] != checkpoint.next_window_index
            or row["current_day"] != checkpoint.current_state.day
            or job.current_day != checkpoint.current_state.day
        ):
            raise RepositoryConflictError("provider recovery checkpoint identity is invalid")
        self._validate_checkpoint(job, checkpoint)
        decisions = connection.execute(
            "SELECT * FROM decision_responses WHERE task_key = ?",
            (job.task_key,),
        ).fetchall()
        if any(
            (
                row["window_index"] < checkpoint.next_window_index
                and row["status"] != DecisionResponseStatus.APPLIED.value
            )
            or (
                row["window_index"] >= checkpoint.next_window_index
                and row["status"] != DecisionResponseStatus.SEALED.value
            )
            for row in decisions
        ):
            raise RepositoryConflictError("provider recovery decisions differ from its frontier")

    @staticmethod
    def _stage_response_row(
        connection: sqlite3.Connection,
        *,
        task_key: str,
        window_index: int,
        response: SealedDecision,
        staged_at: datetime,
    ) -> bool:
        """Insert one immutable response inside the caller's active transaction."""

        decision_id = campaign_decision_id(task_key, window_index, response.company_id)
        response_json = response.model_dump_json()
        response_hash = canonical_model_hash(response)
        existing = connection.execute(
            "SELECT * FROM decision_responses WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["task_key"] != task_key
                or existing["window_index"] != window_index
                or existing["company_id"] != response.company_id
                or existing["response_hash"] != response_hash
                or existing["response_json"] != response_json
            ):
                raise RepositoryConflictError("decision identity already has different content")
            return False
        connection.execute(
            """
            INSERT INTO decision_responses (
                decision_id, task_key, window_index, company_id, status,
                response_hash, response_json, staged_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                decision_id,
                task_key,
                window_index,
                response.company_id,
                DecisionResponseStatus.SEALED.value,
                response_hash,
                response_json,
                _dump_time(staged_at),
            ),
        )
        return True

    @staticmethod
    def _validate_checkpoint_transition(
        previous: RunCheckpoint | None,
        current: RunCheckpoint,
    ) -> None:
        """Require one monotonic runtime phase transition without skipped frontiers."""

        if previous is None:
            return
        if (
            previous.run_id != current.run_id
            or previous.config_hash != current.config_hash
            or previous.initial_state != current.initial_state
        ):
            raise RepositoryConflictError("checkpoint immutable identity changed")
        if current.next_window_index < previous.next_window_index:
            raise RepositoryConflictError("checkpoint frontier cannot move backwards")
        if current.next_window_index > previous.next_window_index + 1:
            raise RepositoryConflictError("checkpoint frontier cannot skip a window")
        previous_windows = previous.completed_windows
        if current.completed_windows[: len(previous_windows)] != previous_windows:
            raise RepositoryConflictError("checkpoint rewrote committed window history")
        if current.next_window_index > previous.next_window_index:
            if previous.phase is not CheckpointPhase.DECISIONS_SEALED:
                raise InvalidTransitionError("economic frontier requires sealed decisions")
            if current.phase not in {CheckpointPhase.READY, CheckpointPhase.COMPLETE}:
                raise InvalidTransitionError("committed frontier has an invalid runtime phase")
            pending = previous.pending_window
            if pending is None:
                raise InvalidTransitionError("sealed checkpoint omitted its decision barrier")
            applied = current.completed_windows[-1]
            expected_barrier = {
                field: getattr(pending, field) for field in type(pending).model_fields
            }
            applied_barrier = {
                field: getattr(applied, field) for field in type(pending).model_fields
            }
            if expected_barrier != applied_barrier:
                raise RepositoryConflictError("committed window differs from sealed decisions")
            return
        if previous == current:
            return
        allowed = {
            CheckpointPhase.READY: {
                CheckpointPhase.COLLECTING_DECISIONS,
                CheckpointPhase.COMPLETE,
            },
            CheckpointPhase.COLLECTING_DECISIONS: {
                CheckpointPhase.COLLECTING_DECISIONS,
                CheckpointPhase.DECISIONS_SEALED,
            },
            CheckpointPhase.DECISIONS_SEALED: set(),
            CheckpointPhase.COMPLETE: set(),
        }
        if current.phase not in allowed[previous.phase]:
            raise InvalidTransitionError(
                f"invalid checkpoint phase transition: {previous.phase} -> {current.phase}"
            )
        if previous.collecting_window is not None:
            old = {item.company_id: item for item in previous.collecting_window.decisions}
            if current.collecting_window is not None:
                if (
                    current.collecting_window.expected_company_ids
                    != previous.collecting_window.expected_company_ids
                ):
                    raise RepositoryConflictError("collecting company set changed")
                new = {item.company_id: item for item in current.collecting_window.decisions}
            elif current.pending_window is not None:
                new = {item.company_id: item for item in current.pending_window.decisions}
                if set(new) != set(previous.collecting_window.expected_company_ids):
                    raise InvalidTransitionError("decision barrier sealed before all responses")
            else:
                new = {}
            if any(new.get(company_id) != response for company_id, response in old.items()):
                raise RepositoryConflictError("checkpoint removed or rewrote a staged response")

    @staticmethod
    def _require_live_lease(
        connection: sqlite3.Connection,
        task_key: str,
        lease_owner: str,
        expected_revision: int,
        now: datetime,
    ) -> RunJobRecord:
        row = _require_job_row(connection, task_key)
        if row["revision"] != expected_revision:
            raise RevisionConflictError("Run revision changed")
        if (
            row["status"] != RunJobStatus.RUNNING.value
            or row["lease_owner"] != lease_owner
            or row["lease_until"] <= _dump_time(now)
        ):
            raise LeaseLostError("Run lease is missing, expired, or owned by another worker")
        return CampaignRepository._job_from_row(row)

    @staticmethod
    def _cas_update(
        connection: sqlite3.Connection,
        task_key: str,
        expected_revision: int,
        changes: str,
        values: tuple[object, ...],
    ) -> None:
        changed = connection.execute(
            f"""
            UPDATE run_jobs SET {changes}, revision = revision + 1
            WHERE task_key = ? AND revision = ?
            """,
            (*values, task_key, expected_revision),
        ).rowcount
        if changed != 1:
            raise RevisionConflictError("Run mutation lost its compare-and-swap race")

    @staticmethod
    def _validate_checkpoint(job: RunJobRecord, checkpoint: RunCheckpoint) -> None:
        if checkpoint.run_id != job.run_id or checkpoint.config_hash != job.config_hash:
            raise RepositoryConflictError("checkpoint identity differs from its Run task")
        if checkpoint.current_state.day < job.current_day:
            raise RepositoryConflictError("checkpoint day cannot move backwards")
        company_ids = job.run_payload.company_ids
        if set(checkpoint.current_state.companies) != set(company_ids):
            raise RepositoryConflictError("checkpoint company set differs from its Run task")
        active_ids = tuple(
            company_id
            for company_id in company_ids
            if checkpoint.current_state.companies[company_id].status is CompanyStatus.ACTIVE
        )
        if (
            checkpoint.collecting_window is not None
            and checkpoint.collecting_window.expected_company_ids != active_ids
        ):
            raise RepositoryConflictError("collecting company set differs from active companies")
        if (
            checkpoint.pending_window is not None
            and tuple(item.company_id for item in checkpoint.pending_window.decisions) != active_ids
        ):
            raise RepositoryConflictError("sealed company set differs from active companies")

    @staticmethod
    def _validate_terminal_checkpoint(job: RunJobRecord, checkpoint: RunCheckpoint) -> None:
        reached_horizon = checkpoint.current_state.day >= job.run_payload.total_days
        all_bankrupt = not any(
            company.status is CompanyStatus.ACTIVE
            for company in checkpoint.current_state.companies.values()
        )
        if not (reached_horizon or all_bankrupt):
            raise InvalidTransitionError("complete checkpoint is not at a terminal world state")

    @staticmethod
    def _validate_completion(
        job: RunJobRecord,
        checkpoint: RunCheckpoint,
        result: ArenaRunResult,
        receipt: ReplayReceipt,
    ) -> None:
        CampaignRepository._validate_checkpoint(job, checkpoint)
        if checkpoint.phase is not CheckpointPhase.COMPLETE:
            raise InvalidTransitionError("Run completion requires a complete checkpoint")
        CampaignRepository._validate_terminal_checkpoint(job, checkpoint)
        if (
            result.run_id != job.run_id
            or result.lineup_id != job.lineup_id
            or result.seed != job.seed
            or result.config_hash != job.config_hash
            or receipt.run_id != job.run_id
            or receipt.config_hash != job.config_hash
        ):
            raise RepositoryConflictError("result or replay identity differs from its Run task")
        final_state_hash = canonical_model_hash(checkpoint.current_state)
        if (
            receipt.verified_days != checkpoint.current_state.day - checkpoint.initial_state.day
            or receipt.verified_windows != len(checkpoint.completed_windows)
            or receipt.final_state_hash != final_state_hash
            or receipt.checkpoint_sha256 != canonical_model_hash(checkpoint)
            or receipt.verifier_fingerprints != result.artifact_fingerprints
        ):
            raise RepositoryConflictError("replay receipt differs from the terminal checkpoint")
        run = job.run_payload
        assignments = tuple(
            (item.company_id, item.model_id, item.api_index) for item in result.assignments
        )
        expected_assignments = tuple(
            zip(run.company_ids, run.model_ids, run.api_indices, strict=True)
        )
        if assignments != expected_assignments:
            raise RepositoryConflictError("result assignments differ from the frozen Run task")

    @staticmethod
    def _insert_artifact(
        connection: sqlite3.Connection,
        *,
        table: str,
        task_key: str,
        payload_json: str,
        payload_hash: str,
        created_at: datetime,
    ) -> None:
        if table not in {"run_results", "replay_receipts"}:
            raise ValueError("unsupported Campaign artifact table")
        connection.execute(
            f"""
            INSERT INTO {table} (task_key, payload_hash, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_key) DO NOTHING
            """,
            (task_key, payload_hash, payload_json, _dump_time(created_at)),
        )
        CampaignRepository._require_same_artifact(connection, table, task_key, payload_json)

    @staticmethod
    def _require_same_artifact(
        connection: sqlite3.Connection,
        table: str,
        task_key: str,
        payload_json: str,
    ) -> None:
        if table not in {"run_results", "replay_receipts"}:
            raise ValueError("unsupported Campaign artifact table")
        row = connection.execute(
            f"SELECT payload_json FROM {table} WHERE task_key = ?", (task_key,)
        ).fetchone()
        if row is None or row["payload_json"] != payload_json:
            raise RepositoryConflictError("published artifact identity conflict")

    def _list_artifacts(
        self,
        campaign_id: str,
        table: str,
    ) -> tuple[tuple[str, str], ...]:
        if table not in {"run_results", "replay_receipts"}:
            raise ValueError("unsupported Campaign artifact table")
        with self._storage.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT artifacts.payload_hash, artifacts.payload_json
                FROM {table} AS artifacts
                JOIN run_jobs AS jobs USING(task_key)
                WHERE jobs.campaign_id = ?
                ORDER BY jobs.seed ASC, jobs.lineup_id ASC, jobs.task_key ASC
                """,
                (campaign_id,),
            ).fetchall()
        return tuple((row["payload_hash"], row["payload_json"]) for row in rows)

    @staticmethod
    def _campaign_from_row(row: sqlite3.Row) -> CampaignRecord:
        return CampaignRecord(
            campaign_id=row["campaign_id"],
            spec_hash=row["spec_hash"],
            plan_hash=row["plan_hash"],
            expected_run_count=row["expected_run_count"],
            status=CampaignStatus(row["status"]),
            created_at=_load_time(row["created_at"]),
            updated_at=_load_time(row["updated_at"]),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> RunJobRecord:
        return RunJobRecord(
            task_key=row["task_key"],
            campaign_id=row["campaign_id"],
            run_id=row["run_id"],
            lineup_id=row["lineup_id"],
            seed=row["seed"],
            config_hash=row["config_hash"],
            run_payload_json=row["run_payload_json"],
            status=RunJobStatus(row["status"]),
            revision=row["revision"],
            current_day=row["current_day"],
            attempt=row["attempt"],
            attempt_limit=row["attempt_limit"],
            pause_requested=bool(row["pause_requested"]),
            stop_reason=(RunStopReason(row["stop_reason"]) if row["stop_reason"] else None),
            retry_not_before=(
                _load_time(row["retry_not_before"]) if row["retry_not_before"] else None
            ),
            lease_owner=row["lease_owner"],
            lease_until=_load_time(row["lease_until"]) if row["lease_until"] else None,
            last_error=row["last_error"],
            created_at=_load_time(row["created_at"]),
            updated_at=_load_time(row["updated_at"]),
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> DecisionResponseRecord:
        return DecisionResponseRecord(
            decision_id=row["decision_id"],
            task_key=row["task_key"],
            window_index=row["window_index"],
            company_id=row["company_id"],
            status=DecisionResponseStatus(row["status"]),
            response_hash=row["response_hash"],
            response=SealedDecision.model_validate_json(row["response_json"]),
            staged_at=_load_time(row["staged_at"]),
            applied_at=_load_time(row["applied_at"]) if row["applied_at"] else None,
        )


def _job_spec(job: RunJobRecord) -> RunJobSpec:
    return RunJobSpec.model_validate(job.model_dump(include=set(RunJobSpec.model_fields)))


def _require_row(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown Campaign: {campaign_id}")
    return row


def _require_job_row(connection: sqlite3.Connection, task_key: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM run_jobs WHERE task_key = ?", (task_key,)).fetchone()
    if row is None:
        raise KeyError(f"unknown Campaign Run: {task_key}")
    return row


def _clean_error(error: str) -> str:
    clean = redact_sensitive_text(error).strip()
    if not clean:
        clean = "unspecified failure"
    return clean[:2_000]


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    require_aware_timestamp(timestamp)
    return timestamp.astimezone(UTC)


def _dump_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    expected_run_count INTEGER NOT NULL CHECK(expected_run_count > 0),
    status TEXT NOT NULL CHECK(status IN ('prepared', 'running', 'incomplete', 'completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_jobs (
    task_key TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    run_id TEXT NOT NULL,
    lineup_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    run_payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'interrupted', 'stopped', 'failed', 'completed'
    )),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    current_day INTEGER NOT NULL DEFAULT 0 CHECK(current_day >= 0),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    attempt_limit INTEGER CHECK(attempt_limit IS NULL OR attempt_limit >= 1),
    pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0, 1)),
    stop_reason TEXT CHECK(stop_reason IS NULL OR stop_reason IN (
        'user_requested', 'provider_retry_exhausted'
    )),
    retry_not_before TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, run_id),
    UNIQUE(campaign_id, lineup_id, seed),
    CHECK(
        (status = 'running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (status != 'running' AND lease_owner IS NULL AND lease_until IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS decision_responses (
    decision_id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL REFERENCES run_jobs(task_key),
    window_index INTEGER NOT NULL CHECK(window_index >= 0),
    company_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('sealed', 'applied')),
    response_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    staged_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE(task_key, window_index, company_id),
    CHECK(
        (status = 'sealed' AND applied_at IS NULL)
        OR (status = 'applied' AND applied_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS failure_history (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL REFERENCES run_jobs(task_key),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    phase TEXT NOT NULL CHECK(phase IN (
        'provider', 'persistence', 'runtime', 'replay', 'configuration', 'unknown'
    )),
    error TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    task_key TEXT PRIMARY KEY REFERENCES run_jobs(task_key),
    frontier INTEGER NOT NULL CHECK(frontier >= 0),
    current_day INTEGER NOT NULL CHECK(current_day >= 0),
    checkpoint_hash TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    world_version INTEGER NOT NULL,
    state_hash TEXT NOT NULL,
    phase TEXT NOT NULL,
    active_companies INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_results (
    task_key TEXT PRIMARY KEY REFERENCES run_jobs(task_key),
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_receipts (
    task_key TEXT PRIMARY KEY REFERENCES run_jobs(task_key),
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_analyses (
    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(campaign_id),
    plan_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_campaign_jobs_claim
    ON run_jobs(campaign_id, status, lease_until, created_at, task_key);
CREATE INDEX IF NOT EXISTS ix_campaign_jobs_retry
    ON run_jobs(campaign_id, status, retry_not_before, seed, task_key);
CREATE INDEX IF NOT EXISTS ix_decision_responses_run_order
    ON decision_responses(task_key, window_index, company_id);
CREATE INDEX IF NOT EXISTS ix_failure_history_run_order
    ON failure_history(task_key, sequence);
"""
