"""Audited HTTP400 recovery upgrade, including already published Run metadata."""

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from ceo_arena.campaign.application import CampaignApplication
from ceo_arena.campaign.models import CampaignRun, FrozenCampaignPlan
from ceo_arena.campaign.recovery_migration import (
    FingerprintChange,
    RecoveryPatchMigration,
    _encode,
    _FileChange,
    _run_config,
    _RunChange,
)
from ceo_arena.evaluation import ArenaRunResult
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.runtime.persistence import CheckpointPhase
from ceo_arena.runtime.replay import ReplayReceipt, ReplayReport

_ALLOWED = frozenset({"src/ceo_arena/runtime/coordinator.py", "src/ceo_arena/runtime/retry.py"})


@dataclass(frozen=True)
class _Publication:
    result: ArenaRunResult
    receipt: ReplayReceipt


class Http400RetryMigration(RecoveryPatchMigration):
    """Rebind recovery provenance while retaining all outcomes and original evidence."""

    patch_id = "http400-ten-attempts-20260913"
    allow_completed = True

    def __init__(self, output: Path, source_root: Path | None = None) -> None:
        super().__init__(output, source_root)
        self._published: dict[str, _Publication] = {}

    @staticmethod
    def _fingerprint_changes(
        before: dict[str, str], after: dict[str, str]
    ) -> tuple[FingerprintChange, ...]:
        changed = RecoveryPatchMigration._fingerprint_changes(before, after)
        if {item.path for item in changed} != _ALLOWED:
            raise ValueError("HTTP400 patch permits exactly coordinator.py and retry.py changes")
        return changed

    def _inspect_run(
        self,
        connection: sqlite3.Connection,
        run: CampaignRun,
        old_plan: FrozenCampaignPlan,
        new_plan: FrozenCampaignPlan,
        changes: list[_FileChange],
    ) -> _RunChange:
        inspected = super()._inspect_run(connection, run, old_plan, new_plan, changes)
        result_row = connection.execute(
            "SELECT * FROM run_results WHERE task_key=?", (run.task_key,)
        ).fetchone()
        receipt_row = connection.execute(
            "SELECT * FROM replay_receipts WHERE task_key=?", (run.task_key,)
        ).fetchone()
        status = connection.execute(
            "SELECT status FROM run_jobs WHERE task_key=?", (run.task_key,)
        ).fetchone()[0]
        directory = self._run_directory(run)
        projection_names = ("result.json", "replay_receipt.json", "final_checkpoint.json")
        if status != "completed":
            if (
                result_row
                or receipt_row
                or any((directory / name).exists() for name in projection_names)
            ):
                raise ValueError("unfinished Run has unexpected published artifacts")
            return inspected
        checkpoint = inspected.old_checkpoint
        if (
            checkpoint.phase is not CheckpointPhase.COMPLETE
            or result_row is None
            or receipt_row is None
        ):
            raise ValueError("completed Run is missing final evidence")
        old_result = ArenaRunResult.model_validate_json(result_row["payload_json"])
        old_receipt = ReplayReceipt.model_validate_json(receipt_row["payload_json"])
        old_config, new_config = _run_config(old_plan, run), _run_config(new_plan, run)
        if (
            canonical_model_hash(old_result) != result_row["payload_hash"]
            or canonical_model_hash(old_receipt) != receipt_row["payload_hash"]
            or old_result != CampaignApplication._result(run, old_config, checkpoint)
        ):
            raise ValueError("published result differs from its original final state")
        old_receipt.validate_checkpoint_inputs(
            checkpoint=checkpoint,
            config_hash=old_config.config_hash,
            runtime_trace_path=directory / "trace.jsonl",
            verifier_fingerprints=old_config.artifact_fingerprints,
        )
        # The base inspection replayed every historical economic transition offline.
        report = ReplayReport(
            run_id=run.run_id,
            verified_windows=len(checkpoint.completed_windows),
            verified_days=inspected.proof.verified_days,
            final_state_hash=inspected.proof.economic_state_hash,
        )
        new_receipt = ReplayReceipt.create_from_checkpoint(
            report,
            checkpoint=inspected.new_checkpoint,
            config_hash=new_config.config_hash,
            runtime_trace_path=directory / "trace.jsonl",
            verifier_fingerprints=new_config.artifact_fingerprints,
        )
        new_result = old_result.model_copy(
            update={
                "config_hash": new_config.config_hash,
                "artifact_fingerprints": new_config.artifact_fingerprints,
            }
        )
        self._published[run.task_key] = _Publication(new_result, new_receipt)
        for name, before, after in (
            ("result.json", old_result, new_result),
            ("replay_receipt.json", old_receipt, new_receipt),
            ("final_checkpoint.json", checkpoint, inspected.new_checkpoint),
        ):
            path = directory / name
            if path.exists():
                if type(before).model_validate_json(path.read_bytes()) != before:
                    raise ValueError(f"completed projection differs from SQLite: {name}")
                changes.append(_FileChange(path, path.read_bytes(), _encode(after)))
        return inspected

    def _build_plan(
        self, old_plan: FrozenCampaignPlan
    ) -> tuple[FrozenCampaignPlan, tuple[FingerprintChange, ...]]:
        self._published.clear()
        return super()._build_plan(old_plan)

    def _update_run(self, connection: sqlite3.Connection, run: _RunChange) -> None:
        super()._update_run(connection, run)
        publication = self._published.get(run.proof.task_key)
        if publication is None:
            return
        for table, value in (
            ("run_results", publication.result),
            ("replay_receipts", publication.receipt),
        ):
            connection.execute(
                f"UPDATE {table} SET payload_json=?, payload_hash=? WHERE task_key=?",
                (value.model_dump_json(), canonical_model_hash(value), run.proof.task_key),
            )

    def _verify_preserved(self, connection: sqlite3.Connection, run: _RunChange) -> None:
        super()._verify_preserved(connection, run)
        publication = self._published.get(run.proof.task_key)
        if publication is not None:
            for table, expected in (
                ("run_results", publication.result),
                ("replay_receipts", publication.receipt),
            ):
                row = connection.execute(
                    f"SELECT payload_json,payload_hash FROM {table} WHERE task_key=?",
                    (run.proof.task_key,),
                ).fetchone()
                if type(expected).model_validate_json(row[0]) != expected or row[
                    1
                ] != canonical_model_hash(expected):
                    raise ValueError("published provenance update failed verification")

    def _receipt_details(self) -> dict[str, JsonValue]:
        return {
            "http400_max_attempts": 10,
            "completed_runs_not_reexecuted": sorted(self._published),
            "completed_results": (
                "Only provenance bindings and offline replay receipts updated; scores and traces "
                "unchanged. Original records remain in the SQLite/file backup."
            ),
            "retry_counts": "Current durable failure counts are retained, never reset.",
        }


class Http400ThreeAttemptMigration(Http400RetryMigration):
    """Lower only the transport retry limit; retain decisions and published results."""

    patch_id = "http400-three-attempts-20260913"

    @staticmethod
    def _fingerprint_changes(
        before: dict[str, str], after: dict[str, str]
    ) -> tuple[FingerprintChange, ...]:
        changed = RecoveryPatchMigration._fingerprint_changes(before, after)
        if {item.path for item in changed} != {"src/ceo_arena/runtime/retry.py"}:
            raise ValueError("HTTP400 limit patch permits only retry.py changes")
        return changed

    def _receipt_details(self) -> dict[str, JsonValue]:
        return {
            **super()._receipt_details(),
            "http400_max_attempts": 3,
            "limit_migration_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
