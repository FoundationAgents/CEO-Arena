"""Explicit, offline migration of existing Runs to the September recovery patch."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, JsonValue, TypeAdapter

from ceo_arena.campaign.checkpoint import _merge_staged_responses
from ceo_arena.campaign.models import CampaignRun, FrozenCampaignPlan
from ceo_arena.campaign.records import RunRecoveryRecord
from ceo_arena.campaign.repository import CampaignRepository
from ceo_arena.campaign.routes import ModelRouteRegistry
from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation import lineup_run_slug
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import StrictModel
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime.allowance import AllowanceProgress
from ceo_arena.runtime.lock import RunDirectoryLock
from ceo_arena.runtime.persistence import AgentAssignment, RunCheckpoint, RunConfig, RunManifest
from ceo_arena.runtime.replay import ExactReplay

PATCH_ID = "harness-recovery-20260913"
INCOMPLETE_MARKER = ".recovery-patch-incomplete.json"
ALLOWED_FINGERPRINT_CHANGES = frozenset(
    {
        "agent_worker/src/runner.ts",
        "agent_worker/src/runner.test.ts",
        "agent_worker/src/conversation.ts",
        "agent_worker/src/continuation.ts",
        "agent_worker/src/continuation.test.ts",
        "agent_worker/src/failure.ts",
        "agent_worker/src/failure.test.ts",
        "agent_worker/src/memory.test.ts",
        "src/ceo_arena/execution.py",
        "src/ceo_arena/evaluation/arena.py",
        "src/ceo_arena/models.py",
        "src/ceo_arena/runtime/allowance.py",
        "src/ceo_arena/runtime/coordinator.py",
        "src/ceo_arena/runtime/performance.py",
        "src/ceo_arena/runtime/retry.py",
    }
)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class FingerprintChange(StrictModel):
    path: str
    before: str | None
    after: str | None


class RunMigrationProof(StrictModel):
    run_id: str
    task_key: str
    day: int
    next_window_index: int
    old_config_hash: str
    new_config_hash: str
    economic_state_hash: str
    checkpoint_without_binding_hash: str
    received_decisions_hash: str
    received_decision_count: int
    verified_days: int
    allowance_files: int
    immutable_files_hash: str


class RecoveryPatchReceipt(StrictModel):
    patch_id: str = PATCH_ID
    created_at: datetime
    campaign_id: str
    old_plan_hash: str
    new_plan_hash: str
    migration_source_sha256: str
    fingerprint_changes: tuple[FingerprintChange, ...]
    runs: tuple[RunMigrationProof, ...]
    changed_files: tuple[str, ...]
    file_backups: dict[str, str]
    database_backup_sha256: str | None = None
    applied: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class _FileChange:
    path: Path
    before: bytes
    after: bytes


@dataclass(frozen=True)
class _RunChange:
    proof: RunMigrationProof
    old_checkpoint: RunCheckpoint
    new_checkpoint: RunCheckpoint
    immutable_files: tuple[Path, ...]


def assert_recovery_ready(output: Path) -> None:
    """Prevent startup across a partially published database/file migration."""
    if (output / INCOMPLETE_MARKER).exists():
        raise RuntimeError("recovery patch is incomplete; inspect its backup before restarting")


class RecoveryPatchMigration:
    """Rebind only Harness provenance, with retained originals and offline replay proof."""

    patch_id = PATCH_ID
    allow_completed = False

    def __init__(self, output: Path, source_root: Path | None = None) -> None:
        self.output = output.resolve(strict=True)
        self.source_root = source_root
        self.database = self.output / "campaign.sqlite3"
        self.plan_path = self.output / "campaign_plan.json"

    def run(
        self, *, apply: bool = False, expected_old_plan_hash: str | None = None
    ) -> RecoveryPatchReceipt:
        """Dry-run by default; apply requires a previously reviewed plan hash and downtime."""
        assert_recovery_ready(self.output)
        old_plan = FrozenCampaignPlan.model_validate_json(self.plan_path.read_bytes())
        if apply and expected_old_plan_hash != old_plan.plan_hash:
            raise ValueError("apply requires the exact reviewed --expected-old-plan-hash")
        with ExitStack() as stack:
            stack.enter_context(RunDirectoryLock(self.output / ".campaign.lock"))
            for run in old_plan.runs:
                stack.enter_context(RunDirectoryLock(self._run_directory(run) / ".run.lock"))
            assert_recovery_ready(self.output)
            new_plan, differences = self._build_plan(old_plan)
            changes = [_FileChange(self.plan_path, self.plan_path.read_bytes(), _encode(new_plan))]
            with closing(self._connect(readonly=True)) as connection:
                self._validate_database(connection, old_plan)
                runs = tuple(
                    self._inspect_run(connection, run, old_plan, new_plan, changes)
                    for run in old_plan.runs
                )
            receipt = RecoveryPatchReceipt(
                patch_id=self.patch_id,
                created_at=datetime.now(UTC),
                campaign_id=old_plan.campaign_id,
                old_plan_hash=old_plan.plan_hash,
                new_plan_hash=new_plan.plan_hash,
                migration_source_sha256=_file_hash(Path(__file__)),
                fingerprint_changes=differences,
                runs=tuple(run.proof for run in runs),
                changed_files=tuple(
                    change.path.relative_to(self.output).as_posix() for change in changes
                ),
                file_backups={
                    change.path.relative_to(
                        self.output
                    ).as_posix(): f"original/{_archive_name(index)}"
                    for index, change in enumerate(changes)
                },
                details=self._receipt_details(),
            )
            return self._apply(old_plan, new_plan, runs, changes, receipt) if apply else receipt

    def _build_plan(
        self, old_plan: FrozenCampaignPlan
    ) -> tuple[FrozenCampaignPlan, tuple[FingerprintChange, ...]]:
        fingerprints = simulation_fingerprints(self.source_root)
        differences = self._fingerprint_changes(old_plan.simulation_fingerprints, fingerprints)
        return old_plan.model_copy(update={"simulation_fingerprints": fingerprints}), differences

    def _receipt_details(self) -> dict[str, JsonValue]:
        return {}

    @staticmethod
    def _fingerprint_changes(
        before: dict[str, str], after: dict[str, str]
    ) -> tuple[FingerprintChange, ...]:
        differences = tuple(
            FingerprintChange(path=path, before=before.get(path), after=after.get(path))
            for path in sorted(before.keys() | after.keys())
            if before.get(path) != after.get(path)
        )
        forbidden = {change.path for change in differences} - ALLOWED_FINGERPRINT_CHANGES
        if forbidden or not differences:
            raise ValueError(
                f"not this Harness-only patch; unexpected changes: {sorted(forbidden)}"
            )
        return differences

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.database.as_uri()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _validate_database(self, connection: sqlite3.Connection, plan: FrozenCampaignPlan) -> None:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("campaign database failed integrity_check")
        campaigns = connection.execute("SELECT * FROM campaigns").fetchall()
        if len(campaigns) != 1 or campaigns[0]["plan_hash"] != plan.plan_hash:
            raise ValueError("database campaign identity differs from the frozen plan")
        if campaigns[0]["campaign_id"] != plan.campaign_id:
            raise ValueError("database campaign ID differs from the frozen plan")
        published_tables = (
            ("campaign_analyses",)
            if self.allow_completed
            else ("run_results", "replay_receipts", "campaign_analyses")
        )
        for table in published_tables:
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise ValueError(
                    "published results exist; this unfinished-run migration refuses them"
                )
        jobs = connection.execute("SELECT * FROM run_jobs").fetchall()
        if {row["task_key"] for row in jobs} != {run.task_key for run in plan.runs}:
            raise ValueError("campaign job set differs from the frozen plan")
        if any(
            (
                row["status"] == "running"
                or (row["status"] == "completed" and not self.allow_completed)
            )
            or row["lease_owner"] is not None
            or row["lease_until"] is not None
            for row in jobs
        ):
            raise ValueError("stop all Runs and release their leases before migration")

    def _inspect_run(
        self,
        connection: sqlite3.Connection,
        run: CampaignRun,
        old_plan: FrozenCampaignPlan,
        new_plan: FrozenCampaignPlan,
        changes: list[_FileChange],
    ) -> _RunChange:
        directory = self._run_directory(run)
        if not self.allow_completed and any(
            (directory / name).exists()
            for name in ("result.json", "final_checkpoint.json", "replay_receipt.json")
        ):
            raise ValueError("final result projections exist; migration refuses to rewrite them")
        manifest_path = directory / "manifest.json"
        manifest = RunManifest.model_validate_json(manifest_path.read_bytes())
        expected = _run_config(old_plan, run)
        if manifest.config != expected:
            raise ValueError(f"Run configuration differs from old plan: {run.run_id}")
        row = connection.execute(
            "SELECT j.config_hash, j.current_day, j.run_payload_json, c.checkpoint_json, "
            "c.checkpoint_hash, c.state_hash FROM run_jobs j JOIN checkpoints c USING(task_key) "
            "WHERE j.task_key=?",
            (run.task_key,),
        ).fetchone()
        if row is None:
            raise ValueError("every migrated Run must have a manifest and authoritative checkpoint")
        checkpoint = RunCheckpoint.model_validate_json(row["checkpoint_json"])
        if (
            row["config_hash"] != expected.config_hash
            or checkpoint.config_hash != expected.config_hash
            or checkpoint.run_id != run.run_id
            or checkpoint.current_state.day != row["current_day"]
            or canonical_model_hash(checkpoint) != row["checkpoint_hash"]
            or canonical_model_hash(checkpoint.current_state) != row["state_hash"]
            or CampaignRun.model_validate_json(row["run_payload_json"]) != run
            or canonical_model_hash(checkpoint.initial_state) != manifest.initial_state_hash
        ):
            raise ValueError(f"checkpoint bindings are inconsistent: {run.run_id}")
        config = _run_config(new_plan, run)
        new_checkpoint = checkpoint.model_copy(update={"config_hash": config.config_hash})
        new_manifest = manifest.model_copy(
            update={"config": config, "config_hash": config.config_hash}
        )
        changes.append(
            _FileChange(manifest_path, manifest_path.read_bytes(), _encode(new_manifest))
        )
        report = ExactReplay(
            MarketWorld(expected.scenario), artifact_directory=directory / "agent-traces"
        ).verify_prefix(_recovery_checkpoint(connection, run.task_key, checkpoint))
        allowance_paths = tuple(sorted((directory / "allowances").glob("*.json")))
        for path in allowance_paths:
            allowance = AllowanceProgress.model_validate_json(path.read_bytes())
            if allowance.config_hash != expected.config_hash:
                raise ValueError("allowance binding differs from frozen Run")
            changes.append(_rebind_file(path, expected.config_hash, config.config_hash))
        projection = directory / "checkpoint.json"
        if projection.exists():
            if RunCheckpoint.model_validate_json(projection.read_bytes()) != checkpoint:
                raise ValueError("JSON checkpoint projection differs from authoritative SQLite")
            changes.append(_rebind_file(projection, expected.config_hash, config.config_hash))
        performance = directory / "performance_status.json"
        if performance.exists():
            changes.append(_rebind_file(performance, expected.config_hash, config.config_hash))
        immutable = tuple(
            sorted(
                path
                for name in ("agent-traces", "agent-memory")
                for path in (directory / name).rglob("*")
                if path.is_file()
            )
        )
        immutable += tuple(
            path
            for name in ("trace.jsonl", "performance.jsonl")
            if (path := directory / name).exists()
        )
        decision_hash, decision_count = _decision_proof(connection, run.task_key)
        return _RunChange(
            proof=RunMigrationProof(
                run_id=run.run_id,
                task_key=run.task_key,
                day=checkpoint.current_state.day,
                next_window_index=checkpoint.next_window_index,
                old_config_hash=expected.config_hash,
                new_config_hash=config.config_hash,
                economic_state_hash=canonical_model_hash(checkpoint.current_state),
                checkpoint_without_binding_hash=_unbound_hash(checkpoint),
                received_decisions_hash=decision_hash,
                received_decision_count=decision_count,
                verified_days=report.verified_days,
                allowance_files=len(allowance_paths),
                immutable_files_hash=_files_hash(immutable, self.output),
            ),
            old_checkpoint=checkpoint,
            new_checkpoint=new_checkpoint,
            immutable_files=immutable,
        )

    def _apply(
        self,
        old_plan: FrozenCampaignPlan,
        new_plan: FrozenCampaignPlan,
        runs: tuple[_RunChange, ...],
        changes: list[_FileChange],
        receipt: RecoveryPatchReceipt,
    ) -> RecoveryPatchReceipt:
        backup = self.output / "recovery-patches" / f"{self.patch_id}-{old_plan.plan_hash[:12]}"
        backup.mkdir(parents=True, exist_ok=False)
        with (
            closing(self._connect(readonly=True)) as original,
            closing(sqlite3.connect(backup / "campaign.sqlite3")) as saved,
        ):
            original.backup(saved)
            if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("migration backup failed integrity_check")
        receipt = receipt.model_copy(
            update={"database_backup_sha256": _file_hash(backup / "campaign.sqlite3")}
        )
        for index, change in enumerate(changes):
            _write_bytes(backup / "original" / _archive_name(index), change.before)
            _write_bytes(backup / "staged" / _archive_name(index), change.after)
        AtomicModelFile(backup / "receipt.json", RecoveryPatchReceipt).save(receipt)
        marker = AtomicModelFile(self.output / INCOMPLETE_MARKER, RecoveryPatchReceipt)
        marker.save(receipt)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            self._validate_database(connection, old_plan)
            for run in runs:
                self._update_run(connection, run)
            for index, change in enumerate(changes):
                if change.path.read_bytes() != change.before:
                    raise ValueError("an artifact changed after migration validation")
                os.replace(backup / "staged" / _archive_name(index), change.path)
                _sync_directory(change.path.parent)
            self._update_campaign(connection, old_plan, new_plan)
            for run in runs:
                self._verify_preserved(connection, run)
            if any(change.path.read_bytes() != change.after for change in changes):
                raise ValueError("published migration artifacts differ from reviewed payloads")
            connection.commit()
        applied = receipt.model_copy(update={"applied": True})
        AtomicModelFile(backup / "receipt.json", RecoveryPatchReceipt).save(applied)
        AtomicModelFile(self.output / "recovery_receipt.json", RecoveryPatchReceipt).save(applied)
        marker.path.unlink()
        _sync_directory(self.output)
        return applied

    def _update_campaign(
        self,
        connection: sqlite3.Connection,
        old_plan: FrozenCampaignPlan,
        new_plan: FrozenCampaignPlan,
    ) -> None:
        connection.execute(
            "UPDATE campaigns SET plan_hash=? WHERE campaign_id=? AND plan_hash=?",
            (new_plan.plan_hash, old_plan.campaign_id, old_plan.plan_hash),
        )

    def _update_run(self, connection: sqlite3.Connection, run: _RunChange) -> None:
        proof = run.proof
        row = connection.execute(
            "SELECT checkpoint_hash FROM checkpoints WHERE task_key=?", (proof.task_key,)
        ).fetchone()
        if row is None or row[0] != canonical_model_hash(run.old_checkpoint):
            raise ValueError("checkpoint changed after migration validation")
        connection.execute(
            "UPDATE checkpoints SET checkpoint_json=?, checkpoint_hash=? WHERE task_key=?",
            (
                run.new_checkpoint.model_dump_json(),
                canonical_model_hash(run.new_checkpoint),
                proof.task_key,
            ),
        )
        count = connection.execute(
            "UPDATE run_jobs SET config_hash=?, revision=revision+1 "
            "WHERE task_key=? AND config_hash=?",
            (proof.new_config_hash, proof.task_key, proof.old_config_hash),
        ).rowcount
        if count != 1:
            raise ValueError("Run binding changed after migration validation")

    def _verify_preserved(self, connection: sqlite3.Connection, run: _RunChange) -> None:
        proof = run.proof
        row = connection.execute(
            "SELECT checkpoint_json FROM checkpoints WHERE task_key=?", (proof.task_key,)
        ).fetchone()
        checkpoint = RunCheckpoint.model_validate_json(row[0])
        if (
            _unbound_hash(checkpoint) != proof.checkpoint_without_binding_hash
            or _decision_proof(connection, proof.task_key)
            != (proof.received_decisions_hash, proof.received_decision_count)
            or _files_hash(run.immutable_files, self.output) != proof.immutable_files_hash
        ):
            raise ValueError(
                "economic state, received decisions or raw traces changed during migration"
            )

    def _run_directory(self, run: CampaignRun) -> Path:
        path = (
            self.output / "runs" / f"seed-{run.seed}" / lineup_run_slug(run.lineup_id)
        ).resolve()
        if not path.is_relative_to(self.output):
            raise ValueError("Run directory escapes the Campaign output")
        return path


def _run_config(plan: FrozenCampaignPlan, run: CampaignRun) -> RunConfig:
    routes = {route.model_id: route for route in plan.routes}
    return RunConfig(
        run_id=run.run_id,
        scenario=plan.scenario_template.model_copy(update={"seed": run.seed}),
        assignments=tuple(
            AgentAssignment(
                company_id=company,
                model_id=model,
                api_index=index,
                harness_id=ModelRouteRegistry.harness_id(model),
                inference=routes[model].inference if index is not None else None,
            )
            for company, model, index in zip(
                run.company_ids, run.model_ids, run.api_indices, strict=True
            )
        ),
        budget=plan.budget,
        provider_endpoint_sha256=plan.provider_endpoint_sha256,
        artifact_fingerprints=plan.simulation_fingerprints,
    )


def _rebind_file(path: Path, before: str, after: str) -> _FileChange:
    original = path.read_bytes()
    value = _JSON_OBJECT.validate_json(original)
    if value.get("config_hash") != before:
        raise ValueError(f"unexpected configuration binding: {path.name}")
    value["config_hash"] = after
    return _FileChange(path, original, (json.dumps(value, ensure_ascii=False) + "\n").encode())


def _decision_proof(connection: sqlite3.Connection, task_key: str) -> tuple[str, int]:
    rows = connection.execute(
        "SELECT * FROM decision_responses WHERE task_key=? ORDER BY decision_id", (task_key,)
    ).fetchall()
    return hashlib.sha256(
        json.dumps([tuple(row) for row in rows], separators=(",", ":")).encode()
    ).hexdigest(), len(rows)


def _recovery_checkpoint(
    connection: sqlite3.Connection, task_key: str, checkpoint: RunCheckpoint
) -> RunCheckpoint:
    job = connection.execute("SELECT * FROM run_jobs WHERE task_key=?", (task_key,)).fetchone()
    rows = connection.execute(
        "SELECT * FROM decision_responses WHERE task_key=? ORDER BY window_index, company_id",
        (task_key,),
    ).fetchall()
    recovery = RunRecoveryRecord(
        job=CampaignRepository._job_from_row(job),
        checkpoint=checkpoint,
        decisions=tuple(CampaignRepository._decision_from_row(row) for row in rows),
    )
    return _merge_staged_responses(checkpoint, recovery)


def _unbound_hash(checkpoint: RunCheckpoint) -> str:
    return canonical_model_hash(checkpoint.model_copy(update={"config_hash": ""}))


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _files_hash(paths: tuple[Path, ...], root: Path) -> str:
    entries = [(path.relative_to(root).as_posix(), _file_hash(path)) for path in paths]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _encode(value: StrictModel) -> bytes:
    return (value.model_dump_json() + "\n").encode()


def _archive_name(index: int) -> str:
    return "campaign_plan.json" if index == 0 else f"{index:05}.json"


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
