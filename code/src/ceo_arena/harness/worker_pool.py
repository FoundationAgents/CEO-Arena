"""Reuse identity-bound worker processes across independent campaign Harnesses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from ceo_arena.runtime._cleanup import finish_cleanup


class WorkerResource(Protocol):
    """A process resource whose cleanup waits for actual process exit."""

    async def close(self) -> None: ...

    async def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerKey:
    """Bind reuse to one Harness instance and its exact company identity."""

    owner_id: str
    company_id: str


@dataclass(slots=True)
class WorkerLease[T: WorkerResource]:
    """Pin a worker until its decision and required cleanup finish."""

    worker: T
    reusable: bool = True

    def invalidate(self) -> None:
        self.reusable = False


@dataclass(slots=True)
class _Entry[T: WorkerResource]:
    key: WorkerKey
    worker: T
    busy: bool = True
    retired: bool = False
    last_used: int = 0


class WorkerPool[T: WorkerResource]:
    """Lease fixed-identity workers with optional resident and idle-cache limits."""

    def __init__(self, capacity: int | None = None, max_idle: int | None = None) -> None:
        if (capacity is not None and capacity < 1) or (max_idle is not None and max_idle < 0):
            raise ValueError("worker capacity must be positive and max_idle non-negative")
        self._capacity = capacity
        self._max_idle = max_idle
        self._entries: dict[WorkerKey, _Entry[T]] = {}
        self._retired_owners: set[str] = set()
        self._condition = asyncio.Condition()
        self._clock = 0
        self._closed = False

    @property
    def capacity(self) -> int | None:
        return self._capacity

    @property
    def max_idle(self) -> int | None:
        return self._max_idle

    @property
    def resident_count(self) -> int:
        """Count reserved, running, idle, and still-terminating worker slots."""

        return len(self._entries)

    @property
    def active_count(self) -> int:
        return sum(entry.busy for entry in self._entries.values())

    @asynccontextmanager
    async def lease(
        self, key: WorkerKey, factory: Callable[[], T]
    ) -> AsyncIterator[WorkerLease[T]]:
        entry = await self._acquire(key, factory)
        lease = WorkerLease(entry.worker)
        try:
            yield lease
        except BaseException:
            lease.invalidate()
            raise
        finally:
            await finish_cleanup(self._release(entry, reusable=lease.reusable))

    async def resize(self, capacity: int | None) -> None:
        """Apply a new cap; active excess workers retire when their leases finish."""

        if capacity is not None and capacity < 1:
            raise ValueError("worker capacity must be positive")
        async with self._condition:
            self._capacity = capacity
            self._condition.notify_all()
        await self.trim()

    async def trim(self) -> None:
        """Reap surplus idle workers without interrupting active decisions."""

        while True:
            async with self._condition:
                idle = self._idle_entries()
                if not idle or (
                    (self._max_idle is None or len(idle) <= self._max_idle)
                    and (self._capacity is None or len(self._entries) <= self._capacity)
                ):
                    return
                entry = min(idle, key=lambda item: item.last_used)
                entry.busy = True
            await finish_cleanup(self._dispose(entry))

    async def retire(self, owner_id: str) -> None:
        """Close one Harness's workers while preserving every other owner's lease."""

        async with self._condition:
            self._retired_owners.add(owner_id)
            for entry in self._entries.values():
                if entry.key.owner_id == owner_id:
                    entry.retired = True
            self._condition.notify_all()
        await finish_cleanup(self._drain(owner_id))

    async def aclose(self) -> None:
        """Drain all leases and workers when their owning campaign shuts down."""

        async with self._condition:
            self._closed = True
            for entry in self._entries.values():
                entry.retired = True
            self._condition.notify_all()
        await finish_cleanup(self._drain())

    async def _acquire(self, key: WorkerKey, factory: Callable[[], T]) -> _Entry[T]:
        while True:
            async with self._condition:
                if self._closed or key.owner_id in self._retired_owners:
                    raise RuntimeError("worker pool or Harness is closed")
                existing = self._entries.get(key)
                if existing is not None and not existing.busy and not existing.retired:
                    existing.busy = True
                    return existing
                if existing is not None and existing.busy:
                    await self._condition.wait()
                    continue
                if existing is None and (
                    self._capacity is None or len(self._entries) < self._capacity
                ):
                    entry = _Entry(key=key, worker=factory())
                    self._entries[key] = entry
                    return entry
                idle = self._idle_entries()
                if idle:
                    entry = min(idle, key=lambda item: item.last_used)
                    entry.busy = True
                else:
                    await self._condition.wait()
                    continue
            await finish_cleanup(self._dispose(entry))

    async def _release(self, entry: _Entry[T], *, reusable: bool) -> None:
        async with self._condition:
            self._clock += 1
            entry.last_used = self._clock
            retire = (
                not reusable
                or entry.retired
                or self._closed
                or (self._capacity is not None and len(self._entries) > self._capacity)
            )
            if not retire:
                entry.busy = False
                idle = self._idle_entries()
                if self._max_idle is not None and len(idle) > self._max_idle:
                    entry = min(idle, key=lambda item: item.last_used)
                    entry.busy = True
                else:
                    self._condition.notify_all()
                    return
                self._condition.notify_all()
        await self._dispose(entry, force=not reusable)

    async def _dispose(self, entry: _Entry[T], *, force: bool = False) -> None:
        reaped = False
        try:
            if force:
                await entry.worker.kill()
            else:
                try:
                    await entry.worker.close()
                except Exception:
                    await entry.worker.kill()
            reaped = True
        finally:
            async with self._condition:
                if reaped:
                    self._entries.pop(entry.key, None)
                else:
                    entry.retired = True
                    entry.busy = False
                self._condition.notify_all()

    async def _drain(self, owner_id: str | None = None) -> None:
        errors: list[Exception] = []
        attempted: set[WorkerKey] = set()
        while True:
            async with self._condition:
                entries = [
                    entry
                    for entry in self._entries.values()
                    if (owner_id is None or entry.key.owner_id == owner_id)
                    and entry.key not in attempted
                ]
                if not entries:
                    break
                idle = [entry for entry in entries if not entry.busy]
                if not idle:
                    await self._condition.wait()
                    continue
                entry = idle[0]
                entry.busy = True
                attempted.add(entry.key)
            try:
                await self._dispose(entry)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("one or more pooled workers failed to close", errors)

    def _idle_entries(self) -> list[_Entry[T]]:
        return [entry for entry in self._entries.values() if not entry.busy]
