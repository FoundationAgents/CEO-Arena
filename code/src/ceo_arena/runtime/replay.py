"""Model-free Exact Replay over sealed decisions and deterministic world transitions."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field

from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import CompanyStatus, StrictModel
from ceo_arena.runtime.persistence import CheckpointPhase, RunCheckpoint
from ceo_arena.runtime.ports import MarketWorld
from ceo_arena.runtime.trace import (
    JsonlTraceSink,
    TraceEvent,
    TraceKind,
    TraceSink,
    decision_barrier_event,
    run_completed_event,
    run_started_event,
    window_committed_event,
)


class ReplayMismatchError(AssertionError):
    """Raised at the first daily or window state divergence."""


class ReplayReport(StrictModel):
    """Successful Exact Replay receipt."""

    run_id: str
    verified_windows: int = Field(ge=0)
    verified_days: int = Field(ge=0)
    final_state_hash: str


class ReplayReceipt(StrictModel):
    """Cached proof that immutable run files passed one full Exact Replay."""

    run_id: str
    config_hash: str
    checkpoint_sha256: str
    runtime_trace_sha256: str
    verified_windows: int = Field(ge=0)
    verified_days: int = Field(ge=0)
    final_state_hash: str
    verifier_fingerprints: dict[str, str]

    @classmethod
    def create(
        cls,
        report: ReplayReport,
        *,
        config_hash: str,
        checkpoint_path: Path,
        runtime_trace_path: Path,
        verifier_fingerprints: dict[str, str],
    ) -> ReplayReceipt:
        """Create a receipt only after `report` has been verified."""

        return cls(
            run_id=report.run_id,
            config_hash=config_hash,
            checkpoint_sha256=_file_sha256(checkpoint_path),
            runtime_trace_sha256=_file_sha256(runtime_trace_path),
            verified_windows=report.verified_windows,
            verified_days=report.verified_days,
            final_state_hash=report.final_state_hash,
            verifier_fingerprints=verifier_fingerprints,
        )

    @classmethod
    def create_from_checkpoint(
        cls,
        report: ReplayReport,
        *,
        checkpoint: RunCheckpoint,
        config_hash: str,
        runtime_trace_path: Path,
        verifier_fingerprints: dict[str, str],
    ) -> ReplayReceipt:
        """Create a receipt for a checkpoint whose authority is not a JSON file."""

        return cls(
            run_id=report.run_id,
            config_hash=config_hash,
            checkpoint_sha256=canonical_model_hash(checkpoint),
            runtime_trace_sha256=_file_sha256(runtime_trace_path),
            verified_windows=report.verified_windows,
            verified_days=report.verified_days,
            final_state_hash=report.final_state_hash,
            verifier_fingerprints=verifier_fingerprints,
        )

    def validate_inputs(
        self,
        *,
        run_id: str,
        config_hash: str,
        checkpoint_path: Path,
        runtime_trace_path: Path,
        verifier_fingerprints: dict[str, str],
    ) -> None:
        """Fail when any replayed immutable input has changed."""

        if (
            self.run_id != run_id
            or self.config_hash != config_hash
            or self.checkpoint_sha256 != _file_sha256(checkpoint_path)
            or self.runtime_trace_sha256 != _file_sha256(runtime_trace_path)
            or self.verifier_fingerprints != verifier_fingerprints
        ):
            raise ReplayMismatchError("cached Exact Replay receipt no longer matches its inputs")

    def validate_checkpoint_inputs(
        self,
        *,
        checkpoint: RunCheckpoint,
        config_hash: str,
        runtime_trace_path: Path,
        verifier_fingerprints: dict[str, str],
    ) -> None:
        """Fail when a database-backed replay input differs from this receipt."""

        if (
            self.run_id != checkpoint.run_id
            or self.config_hash != config_hash
            or self.checkpoint_sha256 != canonical_model_hash(checkpoint)
            or self.runtime_trace_sha256 != _file_sha256(runtime_trace_path)
            or self.verifier_fingerprints != verifier_fingerprints
        ):
            raise ReplayMismatchError("cached Exact Replay receipt no longer matches its inputs")


class ExactReplay:
    """Replay committed CompanyDecision records without owning an AgentHarness."""

    def __init__(
        self,
        world: MarketWorld,
        trace_sink: TraceSink | None = None,
        artifact_directory: Path | None = None,
        runtime_trace_path: Path | None = None,
    ) -> None:
        self._world = world
        self._trace = trace_sink
        self._artifact_directory = artifact_directory.resolve() if artifact_directory else None
        self._runtime_trace_path = runtime_trace_path.resolve() if runtime_trace_path else None

    def verify(self, checkpoint: RunCheckpoint) -> ReplayReport:
        """Replay every committed window and fail at the first hash mismatch."""

        if (
            checkpoint.phase is not CheckpointPhase.COMPLETE
            or checkpoint.pending_window is not None
        ):
            raise ValueError("Exact Replay requires a complete checkpoint")
        self.verify_artifacts(checkpoint)
        report = self._verify_economy(checkpoint)
        if self._trace is not None:
            self._trace.append(
                TraceEvent(
                    record_id=f"{checkpoint.run_id}:replay:verified",
                    kind=TraceKind.REPLAY_VERIFIED,
                    run_id=checkpoint.run_id,
                    end_day=checkpoint.current_state.day,
                    state_hash=report.final_state_hash,
                )
            )
        return report

    def verify_prefix(self, checkpoint: RunCheckpoint) -> ReplayReport:
        """Verify a resumable checkpoint prefix and its received Agent traces."""

        self._verify_harness_traces(checkpoint)
        return self._verify_economy(checkpoint)

    def _verify_economy(self, checkpoint: RunCheckpoint) -> ReplayReport:
        """Replay the committed economic prefix shared by full and import verification."""

        state = checkpoint.initial_state.model_copy(deep=True)
        verified_days = 0
        for expected in checkpoint.completed_windows:
            self._assert_equal(
                self._world.state_hash(state),
                expected.start_state_hash,
                f"window {expected.window_index} start state",
            )
            decisions = {
                item.company_id: item.decision.model_copy(deep=True) for item in expected.decisions
            }
            actual = self._world.advance_window(state.model_copy(deep=True), decisions)
            if len(actual.days) != len(expected.day_fingerprints):
                raise ReplayMismatchError(
                    f"window {expected.window_index} day count mismatch: "
                    f"{len(actual.days)} != {len(expected.day_fingerprints)}"
                )
            for actual_day, expected_day in zip(
                actual.days,
                expected.day_fingerprints,
                strict=True,
            ):
                self._assert_equal(actual_day.day, expected_day.day, "replay day")
                self._assert_equal(
                    actual_day.world_version,
                    expected_day.world_version,
                    f"day {expected_day.day} world version",
                )
                self._assert_equal(
                    actual_day.state_hash,
                    expected_day.state_hash,
                    f"day {expected_day.day} state hash",
                )
                verified_days += 1
            self._assert_equal(
                self._world.state_hash(actual.state),
                expected.end_state_hash,
                f"window {expected.window_index} end state",
            )
            state = actual.state

        final_hash = self._world.state_hash(state)
        self._assert_equal(
            final_hash,
            self._world.state_hash(checkpoint.current_state),
            "terminal checkpoint state",
        )
        report = ReplayReport(
            run_id=checkpoint.run_id,
            verified_windows=len(checkpoint.completed_windows),
            verified_days=verified_days,
            final_state_hash=final_hash,
        )
        return report

    def verify_artifacts(self, checkpoint: RunCheckpoint) -> None:
        """Verify runtime and Agent traces without re-running the economy."""

        if (
            checkpoint.phase is not CheckpointPhase.COMPLETE
            or checkpoint.pending_window is not None
        ):
            raise ValueError("artifact verification requires a complete checkpoint")
        terminal_day = self._world.config.total_days
        current_day = checkpoint.current_state.day
        active_company_exists = any(
            company.status is CompanyStatus.ACTIVE
            for company in checkpoint.current_state.companies.values()
        )
        if current_day > terminal_day or (current_day < terminal_day and active_company_exists):
            raise ValueError("complete checkpoint has not reached a terminal world state")
        self._verify_runtime_trace(checkpoint)
        self._verify_harness_traces(checkpoint)

    def _verify_runtime_trace(self, checkpoint: RunCheckpoint) -> None:
        if self._runtime_trace_path is None:
            return
        initial_hash = self._world.state_hash(checkpoint.initial_state)
        final_hash = self._world.state_hash(checkpoint.current_state)
        expected = [
            run_started_event(
                checkpoint.run_id,
                checkpoint.initial_state.day,
                initial_hash,
            )
        ]
        for window in checkpoint.completed_windows:
            expected.append(decision_barrier_event(checkpoint.run_id, window))
            expected.append(window_committed_event(checkpoint.run_id, window))
        expected.append(
            run_completed_event(
                checkpoint.run_id,
                checkpoint.current_state.day,
                final_hash,
            )
        )
        records = JsonlTraceSink(self._runtime_trace_path).records()
        fields = set(TraceEvent.model_fields)
        actual = [
            TraceEvent.model_validate(record.model_dump(include=fields))
            for record in records
            if record.kind is not TraceKind.REPLAY_VERIFIED
        ]
        if actual != expected:
            raise ReplayMismatchError("runtime trace does not match the checkpoint")
        replay_records = [record for record in records if record.kind is TraceKind.REPLAY_VERIFIED]
        if len(replay_records) > 1 or any(
            record.run_id != checkpoint.run_id
            or record.end_day != checkpoint.current_state.day
            or record.state_hash != final_hash
            for record in replay_records
        ):
            raise ReplayMismatchError("runtime replay trace receipt is invalid")

    def _verify_harness_traces(self, checkpoint: RunCheckpoint) -> None:
        decision_groups = [window.decisions for window in checkpoint.completed_windows]
        if checkpoint.pending_window is not None:
            decision_groups.append(checkpoint.pending_window.decisions)
        if checkpoint.collecting_window is not None:
            decision_groups.append(checkpoint.collecting_window.decisions)
        for decisions in decision_groups:
            for sealed in decisions:
                path = sealed.harness_trace_path
                digest = sealed.harness_trace_sha256
                if (path is None) != (digest is None):
                    raise ReplayMismatchError("Harness trace receipt is incomplete")
                if path is None:
                    continue
                if self._artifact_directory is None:
                    raise ReplayMismatchError("Harness trace directory was not supplied")
                relative = Path(path)
                if relative.is_absolute():
                    raise ReplayMismatchError("Harness trace path must be portable and relative")
                candidate = (self._artifact_directory / relative).resolve()
                if (
                    not candidate.is_relative_to(self._artifact_directory)
                    or not candidate.is_file()
                ):
                    raise ReplayMismatchError(f"Harness trace is missing: {path}")
                actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if actual != digest:
                    raise ReplayMismatchError(f"Harness trace hash mismatch: {path}")

    @staticmethod
    def _assert_equal(actual: object, expected: object, label: str) -> None:
        if actual != expected:
            raise ReplayMismatchError(f"{label} mismatch: {actual!r} != {expected!r}")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
