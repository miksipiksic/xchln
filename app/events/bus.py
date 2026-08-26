"""Per-job append-only event log with live fan-out.

The log is the single source of truth for both the live stream and the replay:
a client connecting to a finished job is handed the exact same recorded events,
so replay is identical by construction rather than by re-deriving the sequence.

Subscribe takes a snapshot of the log and registers the queue under one lock, so
a client attaching mid-run can neither miss an event nor see one twice.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.models import Event, Job

_LOCK = asyncio.Lock()


async def emit(job: Job, event_type: str, data: dict[str, Any]) -> Event:
    async with _LOCK:
        event = Event(seq=len(job.events) + 1, type=event_type, data=data)
        job.events.append(event)
        for queue in list(job.subscribers):
            queue.put_nowait(event)
        return event


async def subscribe(
    job: Job, last_event_id: int | None = None
) -> tuple[list[Event], asyncio.Queue, bool]:
    """Atomically snapshot the backlog and register for what follows.

    The third element says whether the *whole log* already ends in a terminal
    event. It is computed under the lock and reported separately from the
    backlog because Last-Event-ID may filter the terminal event out - a stream
    resuming past the end must still close instead of waiting forever.
    """
    async with _LOCK:
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        events = list(job.events)
        complete = bool(events) and events[-1].terminal
        backlog = events
        if last_event_id is not None:
            backlog = [event for event in events if event.seq > last_event_id]
        return backlog, queue, complete


async def unsubscribe(job: Job, queue: asyncio.Queue) -> None:
    async with _LOCK:
        try:
            job.subscribers.remove(queue)
        except ValueError:
            pass
