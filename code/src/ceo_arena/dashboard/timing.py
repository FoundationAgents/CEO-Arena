"""Incremental, read-only submission times from prompt-free performance records."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ceo_arena.dashboard.models import DashboardCompany
from ceo_arena.runtime.performance import PerformanceEvent


class SubmissionTimes:
    """Index successful receipt persistence; never infer times from file metadata."""

    def __init__(self) -> None:
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._modified_ns = 0
        self._tail = b""
        self._times: dict[tuple[int, str], datetime] = {}

    def get(self, window_index: int, company_id: str) -> datetime | None:
        return self._times.get((window_index, company_id))

    def project(
        self, company: DashboardCompany, settlements: Mapping[int, datetime]
    ) -> DashboardCompany:
        trajectory = tuple(
            entry.model_copy(
                update={
                    "submitted_at": self.get(entry.window_index, company.company_id),
                    "settled_at": settlements.get(entry.window_index)
                    if entry.stage == "completed"
                    else None,
                }
            )
            for entry in company.trajectory
        )
        return company.model_copy(update={"trajectory": trajectory})

    def refresh(self, path: Path) -> None:
        """Read only appended complete lines, bounded to 4 MiB per refresh."""
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            stream.seek(max(0, self._offset - len(self._tail)))
            unchanged_tail = stream.read(len(self._tail)) == self._tail
            if (
                identity != self._identity
                or stat.st_size < self._offset
                or (stat.st_size == self._offset and stat.st_mtime_ns != self._modified_ns)
                or not unchanged_tail
            ):
                self._offset = 0
                self._times.clear()
            self._identity, self._modified_ns = identity, stat.st_mtime_ns
            stream.seek(self._offset)
            limit = self._offset + 4 * 1024 * 1024
            while stream.tell() < limit:
                raw = stream.readline(65_537)
                if len(raw) > 65_536:
                    raise ValueError("performance record exceeds dashboard read limit")
                if not raw or not raw.endswith(b"\n"):
                    break  # A live writer may not have finished its last record.
                self._offset = stream.tell()
                if b'"decision_stage"' not in raw:
                    continue
                try:
                    event = PerformanceEvent.model_validate_json(raw)
                except ValueError:
                    continue  # Optional diagnostics cannot invalidate an economic result.
                if (
                    event.stage == "decision_stage"
                    and event.window_index is not None
                    and event.company_id is not None
                    and event.recorded_at.utcoffset() is not None
                ):
                    self._times.setdefault(
                        (event.window_index, event.company_id), event.recorded_at
                    )
            stream.seek(max(0, self._offset - 128))
            self._tail = stream.read(min(self._offset, 128))
