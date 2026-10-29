"""Shared admission control for model-backed decision work."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Protocol

from ceo_arena.runtime._cleanup import finish_cleanup


class DecisionAdmission(Protocol):
    """Issue a cancellation-safe permit around one decision invocation."""

    def admit(
        self,
        *,
        requires_model_slot: bool,
    ) -> AbstractAsyncContextManager[None]:
        """Return a permit; deterministic decisions bypass model capacity."""


class _UnlimitedPermit(AbstractAsyncContextManager[None]):
    """No-op permit shared by decisions that do not need admission control."""

    def __init__(self, admission: UnlimitedDecisionAdmission | None = None) -> None:
        self._admission = admission
        self._entered = False

    async def __aenter__(self) -> None:
        if self._admission is not None:
            self._admission._active += 1
            self._entered = True
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._admission is not None and self._entered:
            self._admission._active -= 1
            self._entered = False


_UNLIMITED_PERMIT = _UnlimitedPermit()


class UnlimitedDecisionAdmission:
    """Default adapter that starts every decision immediately."""

    def __init__(self) -> None:
        self._active = 0

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def limit(self) -> None:
        return None

    @property
    def waiting_count(self) -> int:
        return 0

    def admit(
        self,
        *,
        requires_model_slot: bool,
    ) -> AbstractAsyncContextManager[None]:
        """Return a no-op permit while preserving the admission interface."""

        return _UnlimitedPermit(self) if requires_model_slot else _UNLIMITED_PERMIT


class _BoundedPermit(AbstractAsyncContextManager[None]):
    """Own one fair, resizable admission permit."""

    def __init__(self, admission: BoundedDecisionAdmission) -> None:
        self._admission = admission
        self._acquired = False

    async def __aenter__(self) -> None:
        await self._admission._acquire()
        self._acquired = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._acquired:
            self._acquired = False
            await finish_cleanup(self._admission._release())


class BoundedDecisionAdmission:
    """Share fair decision slots with a resizable target below one frozen ceiling."""

    def __init__(self, max_inflight_model_decisions: int) -> None:
        if isinstance(max_inflight_model_decisions, bool) or not isinstance(
            max_inflight_model_decisions, int
        ):
            raise TypeError("max_inflight_model_decisions must be an integer")
        if max_inflight_model_decisions <= 0:
            raise ValueError("max_inflight_model_decisions must be positive")
        self._maximum = max_inflight_model_decisions
        self._limit = max_inflight_model_decisions
        self._active = 0
        self._waiters: deque[object] = deque()
        self._condition = asyncio.Condition()

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    async def resize(self, limit: int) -> None:
        """Change future admissions without cancelling an active decision."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self._maximum:
            raise ValueError("decision limit must be within the frozen positive ceiling")
        async with self._condition:
            self._limit = limit
            self._condition.notify_all()

    async def _acquire(self) -> None:
        ticket = object()
        async with self._condition:
            self._waiters.append(ticket)
            try:
                await self._condition.wait_for(
                    lambda: self._active < self._limit and self._waiters[0] is ticket
                )
                self._active += 1
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    async def _release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def admit(
        self,
        *,
        requires_model_slot: bool,
    ) -> AbstractAsyncContextManager[None]:
        """Return a bounded model permit or an immediate deterministic permit."""

        if not requires_model_slot:
            return _UNLIMITED_PERMIT
        return _BoundedPermit(self)
