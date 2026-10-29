"""Run manifests, two-phase checkpoints, and deterministic configuration hashes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from ceo_arena.hashing import canonical_model_hash
from ceo_arena.inference import InferenceSettings, resolve_inference
from ceo_arena.models import (
    AgentBudget,
    AgentUsage,
    CompanyDecision,
    DecisionDiagnostics,
    DecisionWindowResult,
    ProviderFailure,
    ScenarioConfig,
    StrictModel,
    WorldState,
)
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.runtime._atomic import AtomicModelFile
from ceo_arena.runtime._redact import redact_sensitive_text

RUNTIME_PROTOCOL_VERSION = "ceos-runtime-v4"


class AgentAssignment(StrictModel):
    """Public, secret-free identity of one company-controlled Agent."""

    company_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    harness_id: str = Field(min_length=1)
    api_index: int | None = Field(default=None, ge=0)
    inference: InferenceSettings | None = None

    @model_validator(mode="after")
    def validate_route(self) -> AgentAssignment:
        if self.uses_provider != (self.api_index is not None):
            raise ValueError("provider-backed assignments require one API index")
        if self.uses_provider:
            self.inference = resolve_inference(self.model_id, self.inference)
        elif self.inference is not None:
            raise ValueError("non-provider assignments cannot have inference settings")
        return self

    @property
    def uses_provider(self) -> bool:
        """Return whether this assignment invokes the model provider."""

        return self.harness_id.startswith("opencode-native-")


class RunConfig(StrictModel):
    """Complete reproducibility contract for one shared-world run."""

    run_id: str = Field(min_length=1)
    scenario: ScenarioConfig
    assignments: tuple[AgentAssignment, ...]
    budget: AgentBudget = Field(default_factory=AgentBudget)
    provider_endpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_fingerprints: dict[str, str] = Field(default_factory=dict)
    allow_repeated_rule: bool = Field(default=False, exclude_if=lambda repeated: not repeated)

    @model_validator(mode="after")
    def validate_lineup(self) -> RunConfig:
        company_ids = [item.company_id for item in self.assignments]
        model_ids = [
            item.model_id
            for item in self.assignments
            if not (
                self.allow_repeated_rule
                and item.model_id == RULE_MODEL_ID
                and not item.uses_provider
            )
        ]
        if not company_ids:
            raise ValueError("a run requires at least one company")
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("company ids must be unique")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model ids must be unique within a lineup")
        uses_provider = any(item.uses_provider for item in self.assignments)
        if uses_provider and self.provider_endpoint_sha256 is None:
            raise ValueError("provider endpoint identity is missing")
        return self

    @property
    def company_ids(self) -> tuple[str, ...]:
        """Return company ids in their fixed, non-rotating order."""

        return tuple(item.company_id for item in self.assignments)

    @property
    def config_hash(self) -> str:
        """Return the canonical immutable configuration hash."""

        return canonical_model_hash(self)


class RunManifest(StrictModel):
    """Immutable run identity written before any provider is called."""

    protocol_version: str = RUNTIME_PROTOCOL_VERSION
    run_id: str
    config_hash: str
    scenario_hash: str
    initial_state_hash: str
    created_at: datetime
    config: RunConfig

    @model_validator(mode="after")
    def validate_identity(self) -> RunManifest:
        if self.protocol_version != RUNTIME_PROTOCOL_VERSION:
            raise ValueError("run manifest protocol version is unsupported")
        if self.run_id != self.config.run_id:
            raise ValueError("run manifest id differs from its configuration")
        if self.config_hash != self.config.config_hash:
            raise ValueError("run manifest configuration hash is invalid")
        if self.scenario_hash != canonical_model_hash(self.config.scenario):
            raise ValueError("run manifest scenario hash is invalid")
        return self

    @classmethod
    def create(cls, config: RunConfig, initial_state_hash: str) -> RunManifest:
        """Create a manifest from its canonical inputs."""

        return cls(
            run_id=config.run_id,
            config_hash=config.config_hash,
            scenario_hash=canonical_model_hash(config.scenario),
            initial_state_hash=initial_state_hash,
            created_at=datetime.now(UTC),
            config=config,
        )


class SealedDecision(StrictModel):
    """Effective decision persisted before the economy can advance."""

    company_id: str
    agent_status: str
    decision: CompanyDecision
    usage: AgentUsage = Field(default_factory=AgentUsage)
    diagnostics: DecisionDiagnostics = Field(default_factory=DecisionDiagnostics)
    failure: ProviderFailure | None = None
    error: str | None = None
    harness_trace_path: str | None = None
    harness_trace_sha256: str | None = None

    @classmethod
    def from_result(
        cls,
        company_id: str,
        world_version: int,
        result: DecisionWindowResult,
    ) -> SealedDecision:
        """Convert a Harness result into a replayable decision, using no-op on failure."""

        decision = result.decision
        if result.status != "sealed" or decision is None:
            decision = CompanyDecision(company_id=company_id, world_version=world_version)
        else:
            decision = decision.model_copy(deep=True)
        return cls(
            company_id=company_id,
            agent_status=result.status,
            decision=decision,
            usage=result.usage,
            diagnostics=result.diagnostics,
            failure=result.failure,
            error=redact_sensitive_text(result.error),
            harness_trace_path=result.trace_path,
            harness_trace_sha256=result.trace_sha256,
        )


class WindowIdentity(StrictModel):
    """Immutable world identity shared by collecting and sealed barriers."""

    window_index: int = Field(ge=0)
    start_day: int = Field(ge=0)
    start_world_version: int = Field(ge=0)
    start_state_hash: str


class CollectingWindow(WindowIdentity):
    """Incrementally persisted Agent results for one incomplete barrier."""

    expected_company_ids: tuple[str, ...]
    decisions: tuple[SealedDecision, ...] = ()

    @model_validator(mode="after")
    def validate_progress(self) -> CollectingWindow:
        expected = self.expected_company_ids
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("collecting window requires unique expected companies")
        _validate_sealed_decisions(
            self.decisions,
            self.start_world_version,
            expected_company_ids=frozenset(expected),
        )
        return self


class PendingWindow(WindowIdentity):
    """Complete barrier output persisted before its single economic commit."""

    decisions: tuple[SealedDecision, ...]

    @model_validator(mode="after")
    def validate_decisions(self) -> PendingWindow:
        if not self.decisions:
            raise ValueError("sealed window requires company decisions")
        _validate_sealed_decisions(self.decisions, self.start_world_version)
        return self


def _validate_sealed_decisions(
    decisions: tuple[SealedDecision, ...],
    world_version: int,
    *,
    expected_company_ids: frozenset[str] | None = None,
) -> None:
    company_ids = [item.company_id for item in decisions]
    if len(company_ids) != len(set(company_ids)):
        raise ValueError("decision receipts must have unique company ids")
    if expected_company_ids is not None and not set(company_ids) <= expected_company_ids:
        raise ValueError("collecting window contains an unexpected company")
    for item in decisions:
        if item.decision.company_id != item.company_id:
            raise ValueError("sealed decision identity mismatch")
        if item.decision.world_version != world_version:
            raise ValueError("sealed decision targets the wrong world version")


class DayFingerprint(StrictModel):
    """Minimal daily receipt required for Exact Replay verification."""

    day: int = Field(gt=0)
    world_version: int = Field(gt=0)
    state_hash: str


class CompletedWindow(PendingWindow):
    """Committed window receipt with daily and terminal hashes."""

    end_day: int = Field(gt=0)
    end_world_version: int = Field(gt=0)
    day_fingerprints: tuple[DayFingerprint, ...]
    end_state_hash: str

    @model_validator(mode="after")
    def validate_receipt(self) -> CompletedWindow:
        day_count = self.end_day - self.start_day
        if day_count <= 0 or day_count != len(self.day_fingerprints):
            raise ValueError("completed window day receipt length is invalid")
        expected_days = tuple(range(self.start_day + 1, self.end_day + 1))
        if tuple(item.day for item in self.day_fingerprints) != expected_days:
            raise ValueError("completed window day receipts must be contiguous")
        expected_versions = tuple(range(self.start_world_version + 1, self.end_world_version + 1))
        if tuple(item.world_version for item in self.day_fingerprints) != expected_versions:
            raise ValueError("completed window versions must be contiguous")
        if self.end_world_version - self.start_world_version != day_count:
            raise ValueError("completed window day and version counts differ")
        if self.day_fingerprints[-1].state_hash != self.end_state_hash:
            raise ValueError("completed window terminal hashes differ")
        return self


class CheckpointPhase(StrEnum):
    READY = "ready"
    COLLECTING_DECISIONS = "collecting_decisions"
    DECISIONS_SEALED = "decisions_sealed"
    COMPLETE = "complete"


class RunCheckpoint(StrictModel):
    """Atomic two-phase checkpoint sufficient for resume and Exact Replay."""

    run_id: str
    config_hash: str
    phase: CheckpointPhase
    initial_state: WorldState
    current_state: WorldState
    next_window_index: int = Field(ge=0)
    completed_windows: tuple[CompletedWindow, ...] = ()
    collecting_window: CollectingWindow | None = None
    pending_window: PendingWindow | None = None

    @model_validator(mode="after")
    def validate_phase(self) -> RunCheckpoint:
        if self.phase == CheckpointPhase.COLLECTING_DECISIONS and self.collecting_window is None:
            raise ValueError("collecting_decisions checkpoint requires collecting_window")
        if (
            self.phase != CheckpointPhase.COLLECTING_DECISIONS
            and self.collecting_window is not None
        ):
            raise ValueError("collecting_window is only valid in collecting_decisions phase")
        if self.phase == CheckpointPhase.DECISIONS_SEALED and self.pending_window is None:
            raise ValueError("decisions_sealed checkpoint requires pending_window")
        if self.phase != CheckpointPhase.DECISIONS_SEALED and self.pending_window is not None:
            raise ValueError("pending_window is only valid in decisions_sealed phase")
        if self.next_window_index != len(self.completed_windows):
            raise ValueError("next_window_index must equal committed window count")
        indices = tuple(item.window_index for item in self.completed_windows)
        if indices != tuple(range(len(indices))):
            raise ValueError("completed window indices must be contiguous")
        if self.pending_window is not None:
            if self.pending_window.window_index != self.next_window_index:
                raise ValueError("pending window index must equal next_window_index")
            if self.pending_window.start_day != self.current_state.day:
                raise ValueError("pending window day must match current state")
            if self.pending_window.start_world_version != self.current_state.version:
                raise ValueError("pending window version must match current state")
        if self.collecting_window is not None:
            if self.collecting_window.window_index != self.next_window_index:
                raise ValueError("collecting window index must equal next_window_index")
            if self.collecting_window.start_day != self.current_state.day:
                raise ValueError("collecting window day must match current state")
            if self.collecting_window.start_world_version != self.current_state.version:
                raise ValueError("collecting window version must match current state")
        expected_day = self.initial_state.day
        expected_version = self.initial_state.version
        expected_hash = canonical_model_hash(self.initial_state)
        for window in self.completed_windows:
            if (
                window.start_day != expected_day
                or window.start_world_version != expected_version
                or window.start_state_hash != expected_hash
            ):
                raise ValueError("completed windows do not form one contiguous state chain")
            expected_day = window.end_day
            expected_version = window.end_world_version
            expected_hash = window.end_state_hash
        if (
            self.current_state.day != expected_day
            or self.current_state.version != expected_version
            or canonical_model_hash(self.current_state) != expected_hash
        ):
            raise ValueError("checkpoint current state differs from its window receipts")
        if (
            self.pending_window is not None
            and self.pending_window.start_state_hash != expected_hash
        ):
            raise ValueError("pending window state hash differs from the checkpoint")
        if (
            self.collecting_window is not None
            and self.collecting_window.start_state_hash != expected_hash
        ):
            raise ValueError("collecting window state hash differs from the checkpoint")
        return self


class RunManifestStore:
    """Create-once manifest store that rejects configuration drift."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = AtomicModelFile(path, RunManifest)

    def ensure(self, expected: RunManifest) -> RunManifest:
        """Write a new manifest or validate the immutable existing one."""

        existing = self._file.load()
        if existing is None:
            self._file.save(expected)
            return expected
        if existing.model_dump(exclude={"created_at"}) != expected.model_dump(
            exclude={"created_at"}
        ):
            raise ValueError("existing run manifest does not match requested run")
        return existing


class AtomicCheckpointStore:
    """Window snapshots plus FULL-durable individual receipts, overlaid on recovery."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = AtomicModelFile(path, RunCheckpoint)
        self.journal_path = path.with_name("decisions.sqlite3")
        self._collecting_identity: tuple[str, str, int, int, int, str, tuple[str, ...]] | None = (
            None
        )

    def load(self, *, run_id: str, config_hash: str) -> RunCheckpoint | None:
        """Load a matching checkpoint and reject accidental cross-run resume."""

        checkpoint = self._file.load()
        if checkpoint is None:
            return None
        if checkpoint.run_id != run_id or checkpoint.config_hash != config_hash:
            raise ValueError("checkpoint identity does not match run configuration")
        self._set_frontier(checkpoint)
        window = checkpoint.collecting_window
        if window is not None and self.journal_path.exists():
            responses = {response.company_id: response for response in window.decisions}
            with self._journal(write=False) as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='received_decisions'"
                    ).fetchone()
                    is None
                ):
                    return checkpoint
                rows = connection.execute(
                    "SELECT config_hash, start_state_hash, company_id, payload_json "
                    "FROM received_decisions WHERE run_id=? AND window_index=?",
                    (run_id, window.window_index),
                )
                for config, state_hash, company_id, payload in rows:
                    if config != config_hash or state_hash != window.start_state_hash:
                        raise ValueError("decision journal differs from the frozen window")
                    response = SealedDecision.model_validate_json(payload)
                    self._validate_response(window, response)
                    if company_id != response.company_id:
                        raise ValueError("decision journal company identity mismatch")
                    if company_id in responses and responses[company_id] != response:
                        raise ValueError("decision journal conflicts with a saved receipt")
                    responses[company_id] = response
            collecting = window.model_copy(
                update={
                    "decisions": tuple(
                        responses[key] for key in window.expected_company_ids if key in responses
                    )
                }
            )
            checkpoint = checkpoint.model_copy(update={"collecting_window": collecting})
        return checkpoint

    def save(self, checkpoint: RunCheckpoint) -> None:
        """Atomically publish the full replayable checkpoint."""

        self._file.save(checkpoint)
        self._set_frontier(checkpoint)
        if self.journal_path.exists():
            completed = checkpoint.next_window_index - 1
            if checkpoint.pending_window is not None:
                completed = checkpoint.pending_window.window_index
            with self._journal(write=True) as connection:
                connection.execute(
                    "DELETE FROM received_decisions "
                    "WHERE run_id=? AND config_hash=? AND window_index<=?",
                    (checkpoint.run_id, checkpoint.config_hash, completed),
                )

    def stage_decision(
        self,
        *,
        run_id: str,
        config_hash: str,
        window: CollectingWindow,
        response: SealedDecision,
    ) -> None:
        """Commit one receipt once; never serialize the world or prior windows here."""
        identity = self._window_binding(run_id, config_hash, window)
        if identity != self._collecting_identity:
            raise ValueError("decision does not belong to the current collecting checkpoint")
        self._validate_response(window, response)
        with self._journal(write=True) as connection:
            old = connection.execute(
                "SELECT config_hash, start_state_hash, payload_json FROM received_decisions "
                "WHERE run_id=? AND window_index=? AND company_id=?",
                (run_id, window.window_index, response.company_id),
            ).fetchone()
            if old is not None:
                if (
                    old[0] != config_hash
                    or old[1] != window.start_state_hash
                    or SealedDecision.model_validate_json(old[2]) != response
                ):
                    raise ValueError("received decision is immutable and already differs")
                return
            connection.execute(
                "INSERT INTO received_decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    config_hash,
                    window.window_index,
                    window.start_state_hash,
                    response.company_id,
                    response.model_dump_json(),
                ),
            )

    def _set_frontier(self, checkpoint: RunCheckpoint) -> None:
        window = checkpoint.collecting_window
        self._collecting_identity = (
            self._window_binding(checkpoint.run_id, checkpoint.config_hash, window)
            if window is not None
            else None
        )

    @staticmethod
    def _window_binding(
        run_id: str, config_hash: str, window: CollectingWindow
    ) -> tuple[str, str, int, int, int, str, tuple[str, ...]]:
        return (
            run_id,
            config_hash,
            window.window_index,
            window.start_day,
            window.start_world_version,
            window.start_state_hash,
            window.expected_company_ids,
        )

    @staticmethod
    def _validate_response(window: CollectingWindow, response: SealedDecision) -> None:
        _validate_sealed_decisions(
            (response,),
            window.start_world_version,
            expected_company_ids=frozenset(window.expected_company_ids),
        )

    @contextmanager
    def _journal(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if write:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        address = (
            str(self.journal_path) if write else self.journal_path.resolve().as_uri() + "?mode=ro"
        )
        connection = sqlite3.connect(address, uri=not write, timeout=30)
        try:
            if write:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS received_decisions ("
                    "run_id TEXT NOT NULL, config_hash TEXT NOT NULL, "
                    "window_index INTEGER NOT NULL, start_state_hash TEXT NOT NULL, "
                    "company_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
                    "PRIMARY KEY(run_id, window_index, company_id))"
                )
            with connection:
                yield connection
        finally:
            connection.close()


class CheckpointStore(Protocol):
    """Persistence seam for one typed resumable run frontier."""

    def load(self, *, run_id: str, config_hash: str) -> RunCheckpoint | None:
        """Load one matching checkpoint, or return None before the first commit."""

        ...

    def save(self, checkpoint: RunCheckpoint) -> None:
        """Publish one complete validated checkpoint frontier."""

        ...

    def stage_decision(
        self,
        *,
        run_id: str,
        config_hash: str,
        window: CollectingWindow,
        response: SealedDecision,
    ) -> None:
        """Durably record one current-window receipt for load() to overlay."""
        ...
