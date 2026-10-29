"""One explicitly authorized Sol response-label migration, never a global identity bypass."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from ceo_arena.campaign.compiler import CampaignCompiler
from ceo_arena.campaign.models import CampaignRun, FrozenCampaignPlan
from ceo_arena.campaign.recovery_migration import (
    FingerprintChange,
    RecoveryPatchMigration,
    _encode,
    _file_hash,
    _FileChange,
    _files_hash,
    _RunChange,
)
from ceo_arena.campaign.spec import ExperimentSpec
from ceo_arena.economy import MarketWorld
from ceo_arena.harness.bun import resolve_bun_executable
from ceo_arena.harness.wire import WorkerDecisionRequest
from ceo_arena.models import DecisionWindowRequest, ProviderFailure, StrictModel
from ceo_arena.provenance import simulation_fingerprints
from ceo_arena.runtime.allowance import AllowanceProgress

SOL_MODEL = "gpt-5.6-sol"
SOL_ALIAS = "gpt-6-sol"
_MISMATCH = f"Requested {SOL_MODEL}; provider reported {SOL_ALIAS}"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class _ArtifactProof(StrictModel):
    task_key: str
    ignored_fields: tuple[str, ...]
    preserved_sha256: str


class SolAliasMigration(RecoveryPatchMigration):
    """Preserve history while accepting one user-authorized response label for Sol."""

    patch_id = "sol-return-alias-20260913"

    def __init__(
        self,
        output: Path,
        old_spec: ExperimentSpec,
        new_spec: ExperimentSpec,
        source_root: Path | None = None,
    ) -> None:
        super().__init__(output, source_root)
        self.source_root = (source_root or Path(__file__).resolve().parents[3]).resolve()
        self.old_spec = old_spec
        self.new_spec = new_spec
        self._validate_specs()
        self._resumed: dict[str, str] = {}
        self._journals: dict[str, JsonValue] = {}
        self._failures: dict[str, JsonValue] = {}
        self._failure_history: dict[str, str] = {}
        self._artifact_proofs: dict[str, _ArtifactProof] = {}
        self._stale: list[str] = []
        self._legacy: list[str] = []
        self._helper = self.source_root / "agent_worker/scripts/sol-alias-binding.ts"
        self._bun = resolve_bun_executable(self.source_root)

    def _validate_specs(self) -> None:
        before, after = (
            self.old_spec.inference.get(SOL_MODEL),
            self.new_spec.inference.get(SOL_MODEL),
        )
        if before is None or after is None:
            raise ValueError("Sol must exist in both old and new specifications")
        if before.expected_model_ids != (SOL_MODEL,) or after.expected_model_ids != (
            SOL_MODEL,
            SOL_ALIAS,
        ):
            raise ValueError("only the exact reviewed Sol response alias is authorized")
        if after.model_copy(update={"expected_model_ids": before.expected_model_ids}) != before:
            raise ValueError("Sol inference changed outside expected_model_ids")
        restored = self.new_spec.model_copy(
            update={
                "inference": {**self.new_spec.inference, SOL_MODEL: before},
            }
        )
        if restored != self.old_spec:
            raise ValueError("specification changed outside Sol expected_model_ids")

    def _build_plan(
        self, old_plan: FrozenCampaignPlan
    ) -> tuple[FrozenCampaignPlan, tuple[FingerprintChange, ...]]:
        self._resumed.clear()
        self._journals.clear()
        self._failures.clear()
        self._failure_history.clear()
        self._artifact_proofs.clear()
        self._stale.clear()
        self._legacy.clear()
        fingerprints = simulation_fingerprints(self.source_root)
        if fingerprints != old_plan.simulation_fingerprints:
            raise ValueError(
                "Sol alias migration must not change any simulation source fingerprints"
            )
        if self._compile(self.old_spec, old_plan) != old_plan:
            raise ValueError("old specification does not reproduce the frozen plan")
        return self._compile(self.new_spec, old_plan), ()

    @staticmethod
    def _compile(spec: ExperimentSpec, plan: FrozenCampaignPlan) -> FrozenCampaignPlan:
        return CampaignCompiler().compile(
            spec,
            scenario=plan.scenario_template,
            budget=plan.budget,
            simulation_fingerprints=plan.simulation_fingerprints,
        )

    def _validate_database(self, connection: sqlite3.Connection, plan: FrozenCampaignPlan) -> None:
        super()._validate_database(connection, plan)
        if (
            connection.execute("SELECT spec_hash FROM campaigns").fetchone()[0]
            != self.old_spec.spec_hash
        ):
            raise ValueError("database specification binding differs from reviewed old spec")

    def _inspect_run(
        self,
        connection: sqlite3.Connection,
        run: CampaignRun,
        old_plan: FrozenCampaignPlan,
        new_plan: FrozenCampaignPlan,
        changes: list[_FileChange],
    ) -> _RunChange:
        inspected = super()._inspect_run(connection, run, old_plan, new_plan, changes)
        self._failure_history[run.task_key] = self._history_hash(connection, run.task_key)
        if SOL_MODEL not in run.model_ids:
            return inspected
        company = run.company_ids[run.model_ids.index(SOL_MODEL)]
        directory = self._run_directory(run)
        checkpoint = inspected.old_checkpoint
        owner = hashlib.sha256(
            json.dumps([run.run_id, company], separators=(",", ":")).encode()
        ).hexdigest()
        journal = directory / "agent-memory" / owner / "runtime/conversation.json"
        if journal.exists():
            snapshot = _JSON_OBJECT.validate_json(journal.read_bytes())
            version = snapshot.get("world_version")
            if not isinstance(version, int) or version > checkpoint.current_state.version:
                raise ValueError("Sol conversation version is invalid or ahead of the checkpoint")
            if version == checkpoint.current_state.version:
                self._rebind_journal(journal, snapshot, run, company, inspected, old_plan, changes)
            else:
                self._stale.append(journal.relative_to(self.output).as_posix())
        self._unlock_matching_failure(connection, run, company, inspected, changes)
        mutable = {change.path for change in changes}
        immutable = tuple(path for path in inspected.immutable_files if path not in mutable)
        return replace(
            inspected,
            immutable_files=immutable,
            proof=inspected.proof.model_copy(
                update={
                    "immutable_files_hash": _files_hash(immutable, self.output),
                }
            ),
        )

    def _rebind_journal(
        self,
        path: Path,
        snapshot: dict[str, JsonValue],
        run: CampaignRun,
        company: str,
        inspected: _RunChange,
        plan: FrozenCampaignPlan,
        changes: list[_FileChange],
    ) -> None:
        state = inspected.old_checkpoint.current_state
        observations = MarketWorld(
            plan.scenario_template.model_copy(update={"seed": run.seed})
        ).project_observations(state)
        if company not in observations:
            raise ValueError("cannot reconstruct the current Sol observation")
        request = WorkerDecisionRequest(
            request_id="offline-binding-check",
            request=DecisionWindowRequest(
                run_id=run.run_id,
                company_id=company,
                world_version=state.version,
                observation=observations[company],
                budget=plan.budget,
            ),
        )
        bindings = self._conversation_bindings(request)
        if snapshot.get("binding") != bindings.get("before") or not isinstance(
            bindings.get("after"), str
        ):
            raise ValueError(
                f"Sol conversation binding differs from reconstructed frozen window: {run.run_id}"
            )
        updated = {**snapshot, "binding": bindings["after"]}
        relative = path.relative_to(self.output).as_posix()
        self._journals[relative] = bindings
        self._record_artifact_proof(path, run.task_key, "binding")
        changes.append(
            _FileChange(
                path, path.read_bytes(), (json.dumps(updated, ensure_ascii=False) + "\n").encode()
            )
        )

    def _conversation_bindings(self, request: WorkerDecisionRequest) -> dict[str, JsonValue]:
        payload = {
            "message": request.model_dump(mode="json"),
            "before": self.old_spec.inference[SOL_MODEL].model_dump(mode="json"),
            "after": self.new_spec.inference[SOL_MODEL].model_dump(mode="json"),
        }
        result = subprocess.run(
            (self._bun, str(self._helper)),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
            cwd=self.source_root / "agent_worker",
        )
        if result.returncode:
            raise ValueError("offline worker binding calculation failed; no artifacts changed")
        return _JSON_OBJECT.validate_json(result.stdout)

    def _unlock_matching_failure(
        self,
        connection: sqlite3.Connection,
        run: CampaignRun,
        company: str,
        inspected: _RunChange,
        changes: list[_FileChange],
    ) -> None:
        job = connection.execute(
            "SELECT status, last_error FROM run_jobs WHERE task_key=?", (run.task_key,)
        ).fetchone()
        version = inspected.old_checkpoint.current_state.version
        expected_error = f"ProviderIdentityError: {company}: {_MISMATCH}"
        matching_job = job["status"] == "failed" and job["last_error"] == expected_error
        identity = hashlib.sha256(f"{company}:{version}".encode()).hexdigest()
        path = self._run_directory(run) / "allowances" / f"{identity}.json"
        if not path.exists():
            if matching_job:
                raise ValueError("Sol failed job is missing its durable identity failure")
            return
        progress = AllowanceProgress.model_validate_json(path.read_bytes())
        legacy = progress.failure is None and matching_job
        if not legacy and not self._matches_failure(progress.failure, expected_error):
            if matching_job:
                raise ValueError("Sol failed job and allowance failure disagree")
            return
        if not matching_job:
            raise ValueError("Sol durable failure does not match the stopped job")
        directory = self._run_directory(run).relative_to(self.output).as_posix() + "/"
        if not any(journal.startswith(directory) for journal in self._journals):
            raise ValueError("resuming Sol requires a verified current conversation binding")
        history = connection.execute(
            "SELECT error FROM failure_history WHERE task_key=? ORDER BY sequence DESC LIMIT 1",
            (run.task_key,),
        ).fetchone()
        if history is None or history[0] != expected_error:
            raise ValueError("Sol failure must already be preserved in failure_history")
        self._resumed[run.task_key] = job["last_error"]
        self._record_artifact_proof(path, run.task_key, "config_hash", "failure")
        if progress.failure is None:
            self._legacy.append(run.task_key)
        else:
            self._failures[path.relative_to(self.output).as_posix()] = progress.failure.model_dump(
                mode="json"
            )
        updated = progress.model_copy(
            update={"config_hash": inspected.proof.new_config_hash, "failure": None}
        )
        index = next(index for index, change in enumerate(changes) if change.path == path)
        changes[index] = _FileChange(path, path.read_bytes(), _encode(updated))

    @staticmethod
    def _matches_failure(failure: ProviderFailure | None, expected_error: str) -> bool:
        return bool(
            failure
            and failure.category == "identity"
            and not failure.retryable
            and failure.message in {_MISMATCH, expected_error}
        )

    @staticmethod
    def _history_hash(connection: sqlite3.Connection, task_key: str) -> str:
        rows = connection.execute(
            "SELECT * FROM failure_history WHERE task_key=? ORDER BY sequence", (task_key,)
        ).fetchall()
        return hashlib.sha256(json.dumps([tuple(row) for row in rows]).encode()).hexdigest()

    def _record_artifact_proof(self, path: Path, task_key: str, *ignored: str) -> None:
        self._artifact_proofs[path.relative_to(self.output).as_posix()] = _ArtifactProof(
            task_key=task_key,
            ignored_fields=ignored,
            preserved_sha256=self._semantic_hash(path, ignored),
        )

    @staticmethod
    def _semantic_hash(path: Path, ignored: tuple[str, ...]) -> str:
        value = _JSON_OBJECT.validate_json(path.read_bytes())
        preserved = {key: field for key, field in value.items() if key not in ignored}
        return hashlib.sha256(
            json.dumps(
                preserved,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _receipt_details(self) -> dict[str, JsonValue]:
        return {
            "requested_model_unchanged": SOL_MODEL,
            "user_authorized_response_ids": [SOL_MODEL, SOL_ALIAS],
            "identity_equivalence_independently_verified": False,
            "old_spec_hash": self.old_spec.spec_hash,
            "new_spec_hash": self.new_spec.spec_hash,
            "migration_module_sha256": _file_hash(Path(__file__)),
            "binding_helper_sha256": _file_hash(self._helper),
            "resumed_tasks": list(self._resumed),
            "conversation_bindings": self._journals,
            "cleared_failures": self._failures,
            "legacy_missing_failure_metadata": self._legacy,
            "unchanged_stale_conversations": self._stale,
            "preserved_artifact_fields": {
                path: proof.model_dump(mode="json") for path, proof in self._artifact_proofs.items()
            },
        }

    def _update_campaign(
        self,
        connection: sqlite3.Connection,
        old_plan: FrozenCampaignPlan,
        new_plan: FrozenCampaignPlan,
    ) -> None:
        super()._update_campaign(connection, old_plan, new_plan)
        count = connection.execute(
            "UPDATE campaigns SET spec_hash=? WHERE campaign_id=? AND spec_hash=?",
            (self.new_spec.spec_hash, old_plan.campaign_id, self.old_spec.spec_hash),
        ).rowcount
        if count != 1:
            raise ValueError("campaign specification changed during migration")
        for task, error in self._resumed.items():
            count = connection.execute(
                "UPDATE run_jobs SET status='queued', last_error=NULL, pause_requested=0, "
                "stop_reason=NULL, retry_not_before=NULL, revision=revision+1 "
                "WHERE task_key=? AND status='failed' AND last_error=? "
                "AND lease_owner IS NULL AND lease_until IS NULL",
                (task, error),
            ).rowcount
            if count != 1:
                raise ValueError("Sol failure changed during migration")

    def _verify_preserved(self, connection: sqlite3.Connection, run: _RunChange) -> None:
        super()._verify_preserved(connection, run)
        if (
            self._history_hash(connection, run.proof.task_key)
            != self._failure_history[run.proof.task_key]
        ):
            raise ValueError("failure_history changed during migration")
        for relative, proof in self._artifact_proofs.items():
            if proof.task_key != run.proof.task_key:
                continue
            if (
                self._semantic_hash(self.output / relative, proof.ignored_fields)
                != proof.preserved_sha256
            ):
                raise ValueError(
                    "conversation contents or allowance usage changed during migration"
                )
