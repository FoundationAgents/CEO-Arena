"""Append-only, idempotent run trace records."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field

from ceo_arena.models import (
    AgentUsage,
    CompanyDecision,
    DecisionDiagnostics,
    ProviderFailure,
    StrictModel,
)
from ceo_arena.runtime.persistence import CompletedWindow, PendingWindow


class TraceKind(StrEnum):
    RUN_STARTED = "run_started"
    DECISION_BARRIER_SEALED = "decision_barrier_sealed"
    WINDOW_COMMITTED = "window_committed"
    RUN_COMPLETED = "run_completed"
    REPLAY_VERIFIED = "replay_verified"


class DecisionTrace(StrictModel):
    """One company's effective decision and Harness outcome at the barrier."""

    company_id: str
    status: str
    decision: CompanyDecision
    usage: AgentUsage = Field(default_factory=AgentUsage)
    diagnostics: DecisionDiagnostics = Field(default_factory=DecisionDiagnostics)
    failure: ProviderFailure | None = None
    error: str | None = None
    harness_trace_path: str | None = None
    harness_trace_sha256: str | None = None


def run_started_event(run_id: str, start_day: int, state_hash: str) -> TraceEvent:
    """Build the canonical run-start event."""

    return TraceEvent(
        record_id=f"{run_id}:start",
        kind=TraceKind.RUN_STARTED,
        run_id=run_id,
        start_day=start_day,
        state_hash=state_hash,
    )


def decision_barrier_event(run_id: str, pending: PendingWindow) -> TraceEvent:
    """Build the canonical complete decision-barrier event."""

    return TraceEvent(
        record_id=f"{run_id}:window:{pending.window_index}:sealed",
        kind=TraceKind.DECISION_BARRIER_SEALED,
        run_id=run_id,
        window_index=pending.window_index,
        start_day=pending.start_day,
        state_hash=pending.start_state_hash,
        decisions=tuple(
            DecisionTrace(
                company_id=item.company_id,
                status=item.agent_status,
                decision=item.decision,
                usage=item.usage,
                diagnostics=item.diagnostics,
                failure=item.failure,
                error=item.error,
                harness_trace_path=item.harness_trace_path,
                harness_trace_sha256=item.harness_trace_sha256,
            )
            for item in pending.decisions
        ),
    )


def window_committed_event(run_id: str, window: CompletedWindow) -> TraceEvent:
    """Build the canonical economic-window commit event."""

    return TraceEvent(
        record_id=f"{run_id}:window:{window.window_index}:committed",
        kind=TraceKind.WINDOW_COMMITTED,
        run_id=run_id,
        window_index=window.window_index,
        start_day=window.start_day,
        end_day=window.end_day,
        state_hash=window.end_state_hash,
    )


def run_completed_event(run_id: str, end_day: int, state_hash: str) -> TraceEvent:
    """Build the canonical run-completion event."""

    return TraceEvent(
        record_id=f"{run_id}:complete",
        kind=TraceKind.RUN_COMPLETED,
        run_id=run_id,
        end_day=end_day,
        state_hash=state_hash,
    )


class TraceEvent(StrictModel):
    """Idempotently identified event submitted to a TraceSink."""

    record_id: str = Field(min_length=1)
    kind: TraceKind
    run_id: str = Field(min_length=1)
    window_index: int | None = Field(default=None, ge=0)
    start_day: int | None = Field(default=None, ge=0)
    end_day: int | None = Field(default=None, ge=0)
    state_hash: str | None = None
    decisions: tuple[DecisionTrace, ...] = ()


class TraceRecord(TraceEvent):
    """Durable event decorated by the sink with ordering metadata."""

    sequence: int = Field(ge=0)
    recorded_at: datetime


class TraceSink(Protocol):
    """Append-only trace boundary used by coordinator and replay."""

    def append(self, event: TraceEvent) -> TraceRecord:
        """Append once by record_id and return the canonical stored record."""


class JsonlTraceSink:
    """Fsync-backed JSONL sink that never rewrites or truncates prior records."""

    def __init__(
        self,
        path: Path,
        *,
        repair_torn_tail: bool = False,
        tolerate_torn_tail: bool = False,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._repair_torn_tail = repair_torn_tail
        self._tolerate_torn_tail = tolerate_torn_tail
        self._records = self._read_existing()
        self._by_id = {record.record_id: record for record in self._records}

    def append(self, event: TraceEvent) -> TraceRecord:
        """Append an event, or return its existing record after resume."""

        with self._lock:
            existing = self._by_id.get(event.record_id)
            if existing is not None:
                if self._event_payload(existing) != self._event_payload(event):
                    raise ValueError(f"trace record_id collision: {event.record_id}")
                return existing
            record = TraceRecord(
                **event.model_dump(),
                sequence=len(self._records),
                recorded_at=datetime.now(UTC),
            )
            payload = (record.model_dump_json() + "\n").encode("utf-8")
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                if self.path.stat().st_size and not self._ends_with_newline():
                    os.write(descriptor, b"\n")
                self._write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._records.append(record)
            self._by_id[record.record_id] = record
            return record

    def records(self) -> tuple[TraceRecord, ...]:
        """Return an immutable snapshot of all valid trace records."""

        with self._lock:
            return tuple(self._records)

    @staticmethod
    def _event_payload(event: TraceEvent) -> str:
        fields = set(TraceEvent.model_fields)
        return TraceEvent.model_validate(event.model_dump(include=fields)).model_dump_json()

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]

    def _ends_with_newline(self) -> bool:
        with self.path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            return stream.read(1) == b"\n"

    def _read_existing(self) -> list[TraceRecord]:
        if not self.path.exists():
            return []
        payload = self.path.read_bytes()
        lines = payload.splitlines(keepends=True)
        records: list[TraceRecord] = []
        offset = 0
        for index, raw_line in enumerate(lines):
            try:
                record = TraceRecord.model_validate_json(raw_line.rstrip(b"\r\n"))
            except (UnicodeDecodeError, ValueError) as exc:
                torn_tail = index == len(lines) - 1 and not payload.endswith((b"\n", b"\r"))
                if torn_tail and (self._repair_torn_tail or self._tolerate_torn_tail):
                    if self._repair_torn_tail:
                        with self.path.open("r+b") as stream:
                            stream.truncate(offset)
                    break
                raise ValueError(f"invalid trace record at byte {offset}") from exc
            if record.sequence != len(records):
                raise ValueError("trace sequence is not contiguous")
            records.append(record)
            offset += len(raw_line)
        return records


class InMemoryTraceSink:
    """Deterministic TraceSink for unit tests and embedded dry runs."""

    def __init__(self) -> None:
        self._records: list[TraceRecord] = []
        self._by_id: dict[str, TraceRecord] = {}

    def append(self, event: TraceEvent) -> TraceRecord:
        existing = self._by_id.get(event.record_id)
        if existing is not None:
            if JsonlTraceSink._event_payload(existing) != JsonlTraceSink._event_payload(event):
                raise ValueError(f"trace record_id collision: {event.record_id}")
            return existing
        record = TraceRecord(
            **event.model_dump(),
            sequence=len(self._records),
            recorded_at=datetime.now(UTC),
        )
        self._records.append(record)
        self._by_id[record.record_id] = record
        return record

    def records(self) -> tuple[TraceRecord, ...]:
        return tuple(self._records)
