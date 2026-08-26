"""In-memory job store.

One process, no durability requirement in the contract, so there is no database
here. The store is deliberately a narrow interface (create/get/evict) so it
could be backed by Redis without touching anything above it - see SUBMISSION.md
for why that trade was made rather than half-building persistence.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict

from app.config import MAX_JOBS_RETAINED
from app.models import Job, Usage


class JobStore:
    def __init__(self, capacity: int = MAX_JOBS_RETAINED) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._capacity = capacity

    def create(
        self,
        *,
        provider: str,
        max_findings: int,
        content_key: str,
        input_bytes: int,
        chunks: int,
        cache_hit: bool = False,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            provider=provider,
            max_findings=max_findings,
            content_key=content_key,
            usage=Usage(input_bytes=input_bytes, chunks=chunks, cache_hit=cache_hit),
            created_at=time.time(),
            finished=asyncio.Event(),
        )
        self._jobs[job.id] = job
        self._evict()
        return job

    def get(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is not None:
            self._jobs.move_to_end(job_id)
        return job

    def __len__(self) -> int:
        return len(self._jobs)

    def _evict(self) -> None:
        # Bound memory across a multi-day uptime window.
        while len(self._jobs) > self._capacity:
            self._jobs.popitem(last=False)
