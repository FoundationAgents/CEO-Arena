"""Concurrency contracts for live resizing and cancellation-safe admission."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from ceo_arena.runtime.admission import BoundedDecisionAdmission, UnlimitedDecisionAdmission


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@dataclass
class DecisionJob:
    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, admission: BoundedDecisionAdmission, order: list[str]) -> None:
        async with admission.admit(requires_model_slot=True):
            order.append(self.name)
            self.entered.set()
            await self.release.wait()


async def _cancel_all(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_resize_down_preserves_active_decisions_until_below_new_limit() -> None:
    admission = BoundedDecisionAdmission(3)
    jobs = [DecisionJob(str(index)) for index in range(4)]
    order: list[str] = []
    tasks = [asyncio.create_task(job.run(admission, order)) for job in jobs[:3]]
    try:
        await _wait_until(lambda: admission.active_count == 3)
        await admission.resize(1)
        tasks.append(asyncio.create_task(jobs[3].run(admission, order)))
        await _wait_until(lambda: admission.waiting_count == 1)

        assert admission.limit == 1
        assert admission.active_count == 3
        assert all(not task.done() for task in tasks)
        for index in range(2):
            jobs[index].release.set()
            await tasks[index]
            assert admission.active_count == 2 - index
            assert not jobs[3].entered.is_set()

        jobs[2].release.set()
        await _wait_until(jobs[3].entered.is_set)
        assert admission.active_count == 1
        assert order == ["0", "1", "2", "3"]
    finally:
        await _cancel_all(tasks)
    assert admission.active_count == admission.waiting_count == 0


@pytest.mark.asyncio
async def test_resize_up_wakes_waiters_in_fifo_order() -> None:
    admission = BoundedDecisionAdmission(3)
    await admission.resize(1)
    jobs = [DecisionJob(str(index)) for index in range(4)]
    order: list[str] = []
    tasks: list[asyncio.Task[None]] = []
    try:
        for index, job in enumerate(jobs):
            tasks.append(asyncio.create_task(job.run(admission, order)))
            await _wait_until(
                lambda count=index + 1: admission.active_count + admission.waiting_count == count
            )
        await admission.resize(3)
        await _wait_until(lambda: admission.active_count == 3)

        assert order == ["0", "1", "2"]
        assert admission.waiting_count == 1
        assert not jobs[3].entered.is_set()
        jobs[1].release.set()
        await _wait_until(jobs[3].entered.is_set)
        assert order == ["0", "1", "2", "3"]
    finally:
        await _cancel_all(tasks)
    assert admission.active_count == admission.waiting_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_index", [0, 1])
async def test_cancelled_waiter_preserves_fifo_and_does_not_leak(cancelled_index: int) -> None:
    admission = BoundedDecisionAdmission(1)
    jobs = [DecisionJob(str(index)) for index in range(3)]
    order: list[str] = []
    tasks: list[asyncio.Task[None]] = []
    try:
        async with admission.admit(requires_model_slot=True):
            for index, job in enumerate(jobs):
                tasks.append(asyncio.create_task(job.run(admission, order)))
                await _wait_until(lambda count=index + 1: admission.waiting_count == count)
            tasks[cancelled_index].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[cancelled_index]
            assert admission.waiting_count == 2
            assert admission.active_count == 1

        survivors = [index for index in range(3) if index != cancelled_index]
        for index in survivors:
            await _wait_until(jobs[index].entered.is_set)
            assert admission.active_count == 1
            jobs[index].release.set()
            await tasks[index]
        assert order == [str(index) for index in survivors]
    finally:
        await _cancel_all(tasks)
    assert admission.active_count == admission.waiting_count == 0


class ReleaseProbeAdmission(BoundedDecisionAdmission):
    def __init__(self) -> None:
        super().__init__(1)
        self.releasing = asyncio.Event()

    async def _release(self) -> None:
        self.releasing.set()
        await super()._release()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancellation_during_permit_exit_still_releases_capacity(cancellations: int) -> None:
    admission = ReleaseProbeAdmission()
    job = DecisionJob("active")
    task = asyncio.create_task(job.run(admission, []))
    try:
        await _wait_until(job.entered.is_set)
        # Hold the internal lock to force cancellation at the release await point.
        async with admission._condition:
            job.release.set()
            await _wait_until(admission.releasing.is_set)
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert admission.active_count == admission.waiting_count == 0
        async with asyncio.timeout(1):
            async with admission.admit(requires_model_slot=True):
                assert admission.active_count == 1
    finally:
        await _cancel_all([task])


@pytest.mark.asyncio
async def test_rule_admission_bypasses_occupied_resized_capacity() -> None:
    admission = BoundedDecisionAdmission(2)
    await admission.resize(1)
    async with admission.admit(requires_model_slot=True):
        async with asyncio.timeout(1):
            async with admission.admit(requires_model_slot=False):
                assert admission.active_count == 1
                assert admission.waiting_count == 0
    assert admission.active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 4, True, False, 1.5, "2", None])
async def test_resize_rejects_invalid_target_without_changing_capacity(limit: object) -> None:
    admission = BoundedDecisionAdmission(3)
    await admission.resize(2)
    with pytest.raises(ValueError, match="ceiling"):
        await admission.resize(limit)  # type: ignore[arg-type]
    assert admission.limit == 2
    assert admission.active_count == admission.waiting_count == 0


@pytest.mark.asyncio
async def test_unlimited_admission_counts_without_queueing_and_cleans_cancellation() -> None:
    admission = UnlimitedDecisionAdmission()
    release = asyncio.Event()

    async def hold() -> None:
        async with admission.admit(requires_model_slot=True):
            await release.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(50)]
    try:
        await asyncio.sleep(0)
        assert admission.active_count == 50
        assert admission.limit is None and admission.waiting_count == 0
        async with admission.admit(requires_model_slot=False):
            assert admission.active_count == 50
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert admission.active_count == 0
