"""Server-Sent Events rendering.

Replay is exact because a finished job's frames come straight out of its
recorded log - the same objects the live stream emitted - and because heartbeat
comments are only ever sent while *waiting* on a live job. A finished job's
backlog is drained and the stream closes without ever reaching the wait, so two
connections to the same finished job produce byte-identical output.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from app.events import bus
from app.models import Event, Job

HEARTBEAT_SECONDS = 15.0

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Stops nginx and friends from buffering the stream into uselessness.
    "X-Accel-Buffering": "no",
}


def format_event(event: Event) -> str:
    data = json.dumps(event.data, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


def parse_last_event_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def stream_job(job: Job, last_event_id: int | None = None) -> AsyncIterator[str]:
    backlog, queue, complete = await bus.subscribe(job, last_event_id)
    try:
        for event in backlog:
            yield format_event(event)

        if complete:
            return

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield format_event(event)
            if event.terminal:
                return
    finally:
        await bus.unsubscribe(job, queue)
