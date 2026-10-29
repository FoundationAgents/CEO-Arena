"""Complete owned resource cleanup before forwarding task cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine


async def finish_cleanup[T](operation: Coroutine[object, object, T]) -> T:
    """Drain cleanup before propagating even repeated cancellation."""

    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
