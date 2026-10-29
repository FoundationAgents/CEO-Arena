"""Read-only Campaign SQLite projection for the local dashboard."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ceo_arena.campaign.models import (
    CAMPAIGN_PLAN_VERSION,
    DEFAULT_CAMPAIGN_RUN_LIMIT,
    CampaignRun,
    FrozenCampaignPlan,
)
from ceo_arena.campaign.records import (
    DecisionResponseStatus,
    FailurePhase,
    RunJobStatus,
    RunStopReason,
)
from ceo_arena.campaign.repository import (
    CAMPAIGN_DATABASE_SCHEMA_VERSION,
)
from ceo_arena.dashboard.discovery import metadata_files
from ceo_arena.dashboard.models import DashboardResourceStatus, DashboardScheduler
from ceo_arena.evaluation import ArenaRunResult
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.runtime.persistence import CheckpointPhase, RunCheckpoint, SealedDecision
from ceo_arena.runtime.replay import ReplayReceipt

_CAMPAIGN_DATABASE = "campaign.sqlite3"
_CAMPAIGN_PLAN = "campaign_plan.json"
_SCHEDULER_STATUS = "scheduler_status.json"
_MAX_SCHEDULER_BYTES = 64_000
_MAX_PLAN_BYTES = 64_000_000
_MAX_CAMPAIGNS = 32
_MAX_RUNS_PER_CAMPAIGN = DEFAULT_CAMPAIGN_RUN_LIMIT
_MAX_RUN_PAYLOAD_BYTES = 4_000_000
_MAX_CHECKPOINT_BYTES = 128_000_000
_MAX_TOTAL_CHECKPOINT_BYTES = 256_000_000
_MAX_RESULT_BYTES = 16_000_000
_MAX_REPLAY_BYTES = 4_000_000
_MAX_DECISION_BYTES = 1_000_000
_MAX_TOTAL_DECISION_BYTES = 32_000_000


class _ResourceSample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    available_memory_mb: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class _SchedulerSnapshot(BaseModel):
    """Read only the allowlisted subset of the scheduler's local status file."""

    model_config = ConfigDict(extra="ignore")

    timestamp: AwareDatetime
    scheduler_running: bool
    sample_interval_seconds: float = Field(default=10, gt=0, le=3600)
    active_runs: int = Field(ge=0)
    run_limit: int = Field(ge=1)
    active_decisions: int = Field(ge=0)
    resident_workers: int = Field(default=0, ge=0)
    waiting_decisions: int = Field(default=0, ge=0)
    decision_limit: int | None = Field(default=None, ge=1)
    resource_status: DashboardResourceStatus
    resource: _ResourceSample

    @property
    def is_live(self) -> bool:
        age = (datetime.now(UTC) - self.timestamp).total_seconds()
        return self.scheduler_running and 0 <= age <= max(30, 3 * self.sample_interval_seconds)


@dataclass(frozen=True, slots=True)
class CampaignStagedDecision:
    """One validated response staged ahead of the checkpoint frontier."""

    window_index: int
    company_id: str
    response_hash: str
    response: SealedDecision
    status: DecisionResponseStatus


@dataclass(frozen=True, slots=True)
class CampaignRunView:
    """One validated Campaign job without its private task or lease identity."""

    task_key: str
    run_id: str
    lineup_id: str
    seed: int
    model_ids: tuple[str, ...]
    company_ids: tuple[str, ...]
    config_hash: str
    status: RunJobStatus
    revision: int
    pause_requested: bool
    stop_reason: RunStopReason | None
    last_failure_phase: FailurePhase | None
    current_day: int
    updated_at: datetime
    lease_until: datetime | None
    checkpoint_hash: str | None
    checkpoint: RunCheckpoint | None
    result_hash: str | None
    result: ArenaRunResult | None
    replay_hash: str | None
    replay_receipt: ReplayReceipt | None
    staged_decisions: tuple[CampaignStagedDecision, ...]
    retry_not_before: datetime | None = None
    last_error: str | None = None
    attempt: int = 0
    attempt_limit: int | None = None
    decision_revision: str = ""
    checkpoint_phase: str | None = None
    active_companies: int | None = None

    @property
    def lease_expired(self) -> bool:
        """Return whether a stored running lease is no longer live."""

        return (
            self.status is RunJobStatus.RUNNING
            and self.lease_until is not None
            and self.lease_until <= datetime.now(UTC)
        )

    @property
    def cache_key(self) -> str:
        """Return only visible durable state for the per-run dashboard cache."""

        return ":".join(
            (
                str(self.current_day),
                self.checkpoint_hash or "-",
                self.result_hash or "-",
                self.replay_hash or "-",
                self.decision_revision,
            )
        )


@dataclass(frozen=True, slots=True)
class CampaignView:
    """One transactionally consistent Campaign dashboard snapshot."""

    plan: FrozenCampaignPlan
    runs: tuple[CampaignRunView, ...]

    @property
    def active_seed(self) -> int | None:
        """Return the first Seed wave that the scheduler can currently run."""

        runnable_seeds = {run.seed for run in self.runs if run.status.is_scheduler_runnable}
        return next((seed for seed in self.plan.seeds if seed in runnable_seeds), None)

    def waiting_for_previous_seed(self, run: CampaignRunView) -> bool:
        """Return whether strict Seed ordering is the only reason this Run is waiting."""

        if self.plan.execution.schedule != "seed_wave":
            return False
        active_seed = self.active_seed
        if active_seed is None or not run.status.is_scheduler_runnable:
            return False
        waitable = run.status in {RunJobStatus.QUEUED, RunJobStatus.INTERRUPTED} or (
            run.lease_expired and not run.pause_requested
        )
        if not waitable:
            return False
        seed_positions = {seed: index for index, seed in enumerate(self.plan.seeds)}
        return seed_positions[run.seed] > seed_positions[active_seed]


class CampaignReadStore:
    """Validate and read one Campaign through SQLite's read-only URI mode."""

    def __init__(self, root: Path, database: Path, plan_path: Path) -> None:
        self.root = root
        self.database = database
        self.plan_path = plan_path
        self._lock = threading.RLock()
        self._plan_signature: tuple[int, int] | None = None
        self._plan: FrozenCampaignPlan | None = None
        self._scheduler_signature: tuple[int, int] | None = None
        self._scheduler: _SchedulerSnapshot | None = None

    @classmethod
    def discover(cls, root: Path) -> CampaignReadStore | None:
        """Return a reader only for one complete, path-safe Campaign root."""

        resolved_root = root.resolve()
        database = resolved_root / _CAMPAIGN_DATABASE
        plan_path = resolved_root / _CAMPAIGN_PLAN
        if not database.is_file() or not plan_path.is_file():
            return None
        for path in (database, plan_path):
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(resolved_root)
                or _is_link(path)
                or not resolved.is_file()
            ):
                return None
        return cls(resolved_root, database, plan_path)

    @classmethod
    def discover_all(cls, dashboard_root: Path) -> tuple[CampaignReadStore, ...]:
        """Discover bounded, non-linked Campaign roots below one dashboard root."""

        root = dashboard_root.resolve()
        stores: list[CampaignReadStore] = []
        seen: set[Path] = set()
        for plan_path in metadata_files(root, _CAMPAIGN_PLAN):
            if len(stores) >= _MAX_CAMPAIGNS:
                break
            unresolved_root = plan_path.parent.absolute()
            if (
                not unresolved_root.is_relative_to(root)
                or _has_linked_component(unresolved_root, root)
                or _is_link(plan_path)
            ):
                continue
            campaign_root = unresolved_root.resolve()
            if not campaign_root.is_relative_to(root) or campaign_root in seen:
                continue
            store = cls.discover(campaign_root)
            if store is not None:
                stores.append(store)
                seen.add(campaign_root)
        stores.sort(key=lambda store: store.root.as_posix())
        return tuple(stores)

    def load(self, *, include_details: bool = True, run_id: str | None = None) -> CampaignView:
        """Load plan, lifecycle, checkpoints, and results in one read transaction."""

        with self._lock:
            plan = self._load_plan()
            if len(plan.runs) > _MAX_RUNS_PER_CAMPAIGN:
                raise ValueError("Campaign exceeds the dashboard Run limit")
            with self._connect() as connection:
                connection.execute("BEGIN")
                self._validate_database(connection, plan)
                if include_details:
                    self._validate_payload_sizes(connection, plan.campaign_id, run_id)
                rows = connection.execute(
                    """
                    SELECT
                        jobs.task_key,
                        jobs.run_id,
                        jobs.lineup_id,
                        jobs.seed,
                        jobs.config_hash,
                        jobs.run_payload_json,
                        jobs.status,
                        jobs.revision,
                        jobs.pause_requested,
                        jobs.stop_reason,
                        jobs.current_day,
                        jobs.updated_at,
                        jobs.lease_until,
                        jobs.retry_not_before,
                        jobs.last_error,
                        jobs.attempt,
                        jobs.attempt_limit,
                        checkpoints.frontier AS checkpoint_frontier,
                        checkpoints.current_day AS checkpoint_day,
                        checkpoints.checkpoint_hash,
                        checkpoints.phase AS checkpoint_phase,
                        checkpoints.active_companies,
                        (SELECT GROUP_CONCAT(response_hash, ':') FROM decision_responses
                         WHERE task_key = jobs.task_key AND status = 'sealed') AS decision_revision,
                        CASE WHEN :details AND (:run_id IS NULL OR jobs.run_id = :run_id)
                            THEN checkpoints.checkpoint_json END AS checkpoint_json,
                        results.payload_hash,
                        CASE WHEN :details AND (:run_id IS NULL OR jobs.run_id = :run_id)
                            THEN results.payload_json END AS payload_json,
                        receipts.payload_hash AS replay_hash,
                        CASE WHEN :details AND (:run_id IS NULL OR jobs.run_id = :run_id)
                            THEN receipts.payload_json END AS replay_json,
                        (
                            SELECT history.phase FROM failure_history AS history
                            WHERE history.task_key = jobs.task_key
                            ORDER BY history.sequence DESC LIMIT 1
                        ) AS last_failure_phase
                    FROM run_jobs AS jobs
                    LEFT JOIN checkpoints USING(task_key)
                    LEFT JOIN run_results AS results USING(task_key)
                    LEFT JOIN replay_receipts AS receipts USING(task_key)
                    WHERE jobs.campaign_id = :campaign_id
                    ORDER BY jobs.seed ASC, jobs.lineup_id ASC
                    """,
                    {"campaign_id": plan.campaign_id, "details": include_details, "run_id": run_id},
                ).fetchall()
                decision_rows = (
                    connection.execute(
                        """
                    SELECT
                        responses.task_key,
                        responses.window_index,
                        responses.company_id,
                        responses.status,
                        responses.response_hash,
                        responses.response_json
                    FROM decision_responses AS responses
                    JOIN run_jobs AS jobs USING(task_key)
                    WHERE jobs.campaign_id = ? AND responses.status = 'sealed'
                        AND (? IS NULL OR jobs.run_id = ?)
                    ORDER BY responses.task_key, responses.window_index, responses.company_id
                    """,
                        (plan.campaign_id, run_id, run_id),
                    ).fetchall()
                    if include_details
                    else []
                )
                connection.commit()
        expected = {run.run_id: run for run in plan.runs}
        if len(rows) != len(expected):
            raise ValueError("Campaign database Run set differs from its frozen plan")
        staged_by_task: dict[str, list[sqlite3.Row]] = {}
        for row in decision_rows:
            staged_by_task.setdefault(str(row["task_key"]), []).append(row)
        views = tuple(
            self._run_view(
                row,
                expected,
                tuple(staged_by_task.get(str(row["task_key"]), ())),
                include_details=include_details and (run_id is None or row["run_id"] == run_id),
            )
            for row in rows
        )
        if {view.run_id for view in views} != set(expected):
            raise ValueError("Campaign database Run identities differ from its frozen plan")
        return CampaignView(plan=plan, runs=views)

    def revision_signature(self) -> tuple[tuple[int, int], ...]:
        """Describe Campaign database, WAL, plan, and scheduler sample activity."""

        paths = (
            self.plan_path,
            self.database,
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
            self.root / _SCHEDULER_STATUS,
        )
        return tuple(_file_signature(path, self.root) for path in paths)

    def scheduler_status(self, plan: FrozenCampaignPlan) -> DashboardScheduler | None:
        """Project only current bounded telemetry; it is not a Run liveness authority."""
        snapshot = self._load_scheduler()
        if snapshot is None or not snapshot.is_live:
            return None
        policy = plan.execution
        if snapshot.run_limit > policy.max_concurrent_runs or (
            policy.max_inflight_decisions is not None
            and (
                snapshot.decision_limit is None
                or snapshot.decision_limit > policy.max_inflight_decisions
            )
        ):
            return None
        return DashboardScheduler(
            timestamp=snapshot.timestamp,
            active_runs=snapshot.active_runs,
            run_limit=snapshot.run_limit,
            max_runs=policy.max_concurrent_runs,
            active_decisions=snapshot.active_decisions,
            resident_workers=snapshot.resident_workers,
            waiting_decisions=snapshot.waiting_decisions,
            decision_limit=snapshot.decision_limit,
            max_decisions=policy.max_inflight_decisions,
            available_memory_mb=snapshot.resource.available_memory_mb,
            resource_status=snapshot.resource_status,
        )

    def scheduler_is_live(self) -> bool:
        """Change the SSE revision when a stopped or stale sampler loses freshness."""
        snapshot = self._load_scheduler()
        return snapshot is not None and snapshot.is_live

    def _load_scheduler(self) -> _SchedulerSnapshot | None:
        path = self.root / _SCHEDULER_STATUS
        try:
            signature = _file_signature(path, self.root)
            if signature == self._scheduler_signature:
                return self._scheduler
            self._scheduler_signature = signature
            self._scheduler = None
            if not 0 < signature[1] <= _MAX_SCHEDULER_BYTES:
                return None
            with path.open("rb") as stream:
                payload = stream.read(_MAX_SCHEDULER_BYTES + 1)
            if len(payload) <= _MAX_SCHEDULER_BYTES:
                self._scheduler = _SchedulerSnapshot.model_validate_json(payload)
        except (OSError, ValueError):
            self._scheduler = None
        return self._scheduler

    def lease_signature(self) -> str:
        """Return an opaque liveness digest that changes at lease-expiry boundaries."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id, lease_until FROM run_jobs
                    WHERE status = 'running' ORDER BY run_id
                    """
                ).fetchall()
        except (sqlite3.Error, ValueError):
            return "unavailable"
        now = datetime.now(UTC)
        parts: list[str] = []
        for row in rows:
            lease_until = _load_time(row["lease_until"])
            if lease_until is None:
                continue
            parts.append(f"{row['run_id']}:{lease_until <= now}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _load_plan(self) -> FrozenCampaignPlan:
        signature = _file_signature(self.plan_path, self.root)
        if signature == (0, 0):
            raise ValueError("Campaign plan is unavailable")
        if self._plan is not None and signature == self._plan_signature:
            return self._plan
        if signature[1] > _MAX_PLAN_BYTES:
            raise ValueError("Campaign plan exceeds the dashboard read limit")
        safe_plan = self.plan_path.resolve()
        if not safe_plan.is_relative_to(self.root) or _is_link(self.plan_path):
            raise ValueError("Campaign plan path is unsafe")
        text = safe_plan.read_text(encoding="utf-8")
        raw = json.loads(text)
        if (
            not isinstance(raw, dict)
            or raw.get("protocol_version") != CAMPAIGN_PLAN_VERSION
            or not isinstance(raw.get("campaign_id"), str)
            or not isinstance(raw.get("days"), int)
            or isinstance(raw.get("days"), bool)
            or not isinstance(raw.get("seeds"), list)
        ):
            raise ValueError("Campaign plan has an invalid explicit protocol envelope")
        plan = FrozenCampaignPlan.model_validate_json(text)
        self._plan = plan
        self._plan_signature = signature
        return plan

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if _file_signature(self.database, self.root) == (0, 0):
            raise ValueError("Campaign database path is unavailable or unsafe")
        wal_exists = Path(f"{self.database}-wal").is_file()
        shm_exists = Path(f"{self.database}-shm").is_file()
        if wal_exists != shm_exists:
            raise ValueError("Campaign WAL sidecars are in a transient state")
        query = "mode=ro" if wal_exists else "mode=ro&immutable=1"
        uri = f"{self.database.resolve().as_uri()}?{query}"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_database(
        connection: sqlite3.Connection,
        plan: FrozenCampaignPlan,
    ) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != CAMPAIGN_DATABASE_SCHEMA_VERSION:
            raise ValueError("Campaign database schema is unsupported")
        row = connection.execute(
            """
            SELECT plan_hash, expected_run_count
            FROM campaigns WHERE campaign_id = ?
            """,
            (plan.campaign_id,),
        ).fetchone()
        if (
            row is None
            or str(row["plan_hash"]) != plan.plan_hash
            or int(row["expected_run_count"]) != len(plan.runs)
        ):
            raise ValueError("Campaign database identity differs from its frozen plan")

    @staticmethod
    def _validate_payload_sizes(
        connection: sqlite3.Connection, campaign_id: str, run_id: str | None = None
    ) -> None:
        row = connection.execute(
            """
            SELECT
                MAX(length(CAST(jobs.run_payload_json AS BLOB))) AS max_run,
                MAX(length(CAST(checkpoints.checkpoint_json AS BLOB))) AS max_checkpoint,
                COALESCE(SUM(length(CAST(checkpoints.checkpoint_json AS BLOB))), 0)
                    AS total_checkpoint,
                MAX(length(CAST(results.payload_json AS BLOB))) AS max_result,
                MAX(length(CAST(receipts.payload_json AS BLOB))) AS max_replay
            FROM run_jobs AS jobs
            LEFT JOIN checkpoints USING(task_key)
            LEFT JOIN run_results AS results USING(task_key)
            LEFT JOIN replay_receipts AS receipts USING(task_key)
            WHERE jobs.campaign_id = ? AND (? IS NULL OR jobs.run_id = ?)
            """,
            (campaign_id, run_id, run_id),
        ).fetchone()
        limits = (
            (row["max_run"], _MAX_RUN_PAYLOAD_BYTES),
            (row["max_checkpoint"], _MAX_CHECKPOINT_BYTES),
            (row["total_checkpoint"], _MAX_TOTAL_CHECKPOINT_BYTES),
            (row["max_result"], _MAX_RESULT_BYTES),
            (row["max_replay"], _MAX_REPLAY_BYTES),
        )
        if any(value is not None and int(value) > limit for value, limit in limits):
            raise ValueError("Campaign database payload exceeds the dashboard read limit")
        decisions = connection.execute(
            """
            SELECT
                MAX(length(CAST(responses.response_json AS BLOB))) AS maximum,
                COALESCE(SUM(length(CAST(responses.response_json AS BLOB))), 0) AS total
            FROM decision_responses AS responses
            JOIN run_jobs AS jobs USING(task_key)
            WHERE jobs.campaign_id = ? AND responses.status = 'sealed'
                AND (? IS NULL OR jobs.run_id = ?)
            """,
            (campaign_id, run_id, run_id),
        ).fetchone()
        if (
            decisions["maximum"] is not None and int(decisions["maximum"]) > _MAX_DECISION_BYTES
        ) or int(decisions["total"]) > _MAX_TOTAL_DECISION_BYTES:
            raise ValueError("Campaign decision payload exceeds the dashboard read limit")

    @staticmethod
    def _run_view(
        row: sqlite3.Row,
        expected: dict[str, CampaignRun],
        decision_rows: tuple[sqlite3.Row, ...],
        *,
        include_details: bool = True,
    ) -> CampaignRunView:
        run = CampaignRun.model_validate_json(str(row["run_payload_json"]))
        planned = expected.get(run.run_id)
        if planned is None or planned != run:
            raise ValueError("Campaign Run payload differs from its frozen plan")
        if (
            str(row["task_key"]) != run.task_key
            or str(row["run_id"]) != run.run_id
            or str(row["lineup_id"]) != run.lineup_id
            or int(row["seed"]) != run.seed
        ):
            raise ValueError("Campaign Run row identity is invalid")
        config_hash = str(row["config_hash"])
        checkpoint_hash = (
            str(row["checkpoint_hash"]) if row["checkpoint_hash"] is not None else None
        )
        checkpoint = (
            RunCheckpoint.model_validate_json(str(row["checkpoint_json"]))
            if row["checkpoint_json"] is not None
            else None
        )
        if checkpoint is not None and (
            checkpoint_hash != canonical_model_hash(checkpoint)
            or checkpoint.run_id != run.run_id
            or checkpoint.config_hash != config_hash
            or int(row["checkpoint_frontier"]) != checkpoint.next_window_index
            or int(row["checkpoint_day"]) != checkpoint.current_state.day
            or int(row["current_day"]) != checkpoint.current_state.day
        ):
            raise ValueError("Campaign checkpoint identity is invalid")
        result_hash = str(row["payload_hash"]) if row["payload_hash"] is not None else None
        result = (
            ArenaRunResult.model_validate_json(str(row["payload_json"]))
            if row["payload_json"] is not None
            else None
        )
        if result is not None and (
            result_hash != canonical_model_hash(result)
            or result.run_id != run.run_id
            or result.lineup_id != run.lineup_id
            or result.seed != run.seed
            or result.config_hash != config_hash
        ):
            raise ValueError("Campaign result identity is invalid")
        replay_hash = str(row["replay_hash"]) if row["replay_hash"] is not None else None
        replay_receipt = (
            ReplayReceipt.model_validate_json(str(row["replay_json"]))
            if row["replay_json"] is not None
            else None
        )
        if replay_receipt is not None and (
            replay_hash != canonical_model_hash(replay_receipt)
            or replay_receipt.run_id != run.run_id
            or replay_receipt.config_hash != config_hash
        ):
            raise ValueError("Campaign replay receipt identity is invalid")
        status = RunJobStatus(str(row["status"]))
        if include_details and status is RunJobStatus.COMPLETED:
            if (
                result is None
                or replay_receipt is None
                or (checkpoint is not None and checkpoint.phase is not CheckpointPhase.COMPLETE)
            ):
                raise ValueError("completed Campaign Run artifacts are inconsistent")
        elif result is not None or replay_receipt is not None:
            raise ValueError("non-completed Campaign Run contains a published result")
        updated_at = datetime.fromisoformat(str(row["updated_at"]))
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("Campaign Run timestamp must be timezone-aware")
        lease_until = _load_time(row["lease_until"])
        staged_decisions = tuple(
            CampaignReadStore._staged_decision(item, run) for item in decision_rows
        )
        if checkpoint is not None and any(
            (
                decision.window_index < checkpoint.next_window_index
                and decision.status is not DecisionResponseStatus.APPLIED
            )
            or (
                decision.window_index >= checkpoint.next_window_index
                and decision.status is not DecisionResponseStatus.SEALED
            )
            for decision in staged_decisions
        ):
            raise ValueError("Campaign decision status differs from its checkpoint frontier")
        return CampaignRunView(
            task_key=run.task_key,
            run_id=run.run_id,
            lineup_id=run.lineup_id,
            seed=run.seed,
            model_ids=run.model_ids,
            company_ids=run.company_ids,
            config_hash=config_hash,
            status=status,
            revision=int(row["revision"]),
            pause_requested=bool(row["pause_requested"]),
            stop_reason=(
                RunStopReason(str(row["stop_reason"])) if row["stop_reason"] is not None else None
            ),
            last_failure_phase=(
                FailurePhase(str(row["last_failure_phase"]))
                if row["last_failure_phase"] is not None
                else None
            ),
            current_day=int(row["current_day"]),
            updated_at=updated_at,
            lease_until=lease_until,
            checkpoint_hash=checkpoint_hash,
            checkpoint=checkpoint,
            result_hash=result_hash,
            result=result,
            replay_hash=replay_hash,
            replay_receipt=replay_receipt,
            staged_decisions=staged_decisions,
            retry_not_before=_load_time(row["retry_not_before"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            attempt=int(row["attempt"]),
            attempt_limit=int(row["attempt_limit"]) if row["attempt_limit"] is not None else None,
            decision_revision=str(row["decision_revision"] or ""),
            checkpoint_phase=str(row["checkpoint_phase"])
            if row["checkpoint_phase"] is not None
            else None,
            active_companies=int(row["active_companies"])
            if row["active_companies"] is not None
            else None,
        )

    @staticmethod
    def _staged_decision(row: sqlite3.Row, run: CampaignRun) -> CampaignStagedDecision:
        response = SealedDecision.model_validate_json(str(row["response_json"]))
        response_hash = str(row["response_hash"])
        company_id = str(row["company_id"])
        window_index = int(row["window_index"])
        status = DecisionResponseStatus(str(row["status"]))
        if (
            str(row["task_key"]) != run.task_key
            or company_id not in run.company_ids
            or response.company_id != company_id
            or response_hash != canonical_model_hash(response)
            or window_index < 0
        ):
            raise ValueError("Campaign staged decision identity is invalid")
        return CampaignStagedDecision(
            window_index=window_index,
            company_id=company_id,
            response_hash=response_hash,
            response=response,
            status=status,
        )


def _file_signature(path: Path, root: Path) -> tuple[int, int]:
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or _is_link(path) or not resolved.is_file():
        return 0, 0
    try:
        stat = resolved.stat()
    except OSError:
        return 0, 0
    return stat.st_mtime_ns, stat.st_size


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _load_time(value: object) -> datetime | None:
    if value is None:
        return None
    timestamp = datetime.fromisoformat(str(value))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Campaign timestamp must be timezone-aware")
    return timestamp


def _has_linked_component(path: Path, root: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if _is_link(current):
            return True
    return False


__all__ = [
    "CampaignReadStore",
    "CampaignRunView",
    "CampaignStagedDecision",
    "CampaignView",
]
