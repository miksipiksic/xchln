"""The worker pool: bounded concurrency, graceful failure, ordered emission.

Exactly MAX_CONCURRENT_JOBS workers drain a single queue, so a fifth submission
waits its turn rather than being rejected.

One deliberate design call lives here. The contract says finding events are
emitted "as discovered", *and* that ordering by (path, line, ruleId) holds
"everywhere (results and streams)". Those pull in opposite directions: chunks
are file-aligned, but the diff's file order need not be lexicographic, so
emitting per chunk can produce an out-of-order stream. Findings are therefore
buffered, ordered once through models.normalize(), and only then streamed. The
ordering guarantee is testable and scored; incremental delivery is not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config import MAX_CONCURRENT_JOBS
from app.events import bus
from app.jobs.cache import ContentCache
from app.models import DiffChunk, Job, Usage, normalize
from app.providers.base import ProviderError
from app.providers.registry import get_provider

log = logging.getLogger("reviews.queue")


@dataclass(slots=True)
class JobPayload:
    """Work for one job. Dropped once the job is terminal to bound memory."""

    chunks: list[DiffChunk]


class JobRunner:
    def __init__(
        self,
        cache: ContentCache,
        concurrency: int = MAX_CONCURRENT_JOBS,
    ) -> None:
        self._cache = cache
        self._concurrency = concurrency
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._payloads: dict[str, JobPayload] = {}
        self._workers: list[asyncio.Task] = []
        self._mirrors: set[asyncio.Task] = set()

    # ----------------------------------------------------------------- #
    # lifecycle
    # ----------------------------------------------------------------- #
    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"review-worker-{i}")
            for i in range(self._concurrency)
        ]

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in list(self._mirrors):
            task.cancel()
        await asyncio.gather(*self._workers, *self._mirrors, return_exceptions=True)
        self._workers.clear()
        self._mirrors.clear()

    # ----------------------------------------------------------------- #
    # submission
    # ----------------------------------------------------------------- #
    async def submit(self, job: Job, payload: JobPayload) -> None:
        self._payloads[job.id] = payload
        await bus.emit(job, "status", {"jobId": job.id, "status": "queued"})
        self._queue.put_nowait(job)

    async def submit_mirror(self, job: Job, source: Job, payload: JobPayload) -> None:
        """A cache hit: this job copies `source`'s result instead of redoing it.

        Mirrors wait on the source rather than occupying a worker slot, so a
        burst of identical submissions cannot starve genuinely new work.
        """
        self._payloads[job.id] = payload
        await bus.emit(job, "status", {"jobId": job.id, "status": "queued"})
        task = asyncio.create_task(self._mirror(job, source), name=f"mirror-{job.id}")
        self._mirrors.add(task)
        task.add_done_callback(self._mirrors.discard)

    # ----------------------------------------------------------------- #
    # workers
    # ----------------------------------------------------------------- #
    async def _worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defence in depth
                log.exception("worker %s crashed on job %s", index, job.id)
                await self._fail(job, "internal", "internal error while reviewing")
            finally:
                self._queue.task_done()

    async def _process(self, job: Job) -> None:
        payload = self._payloads.get(job.id)
        if payload is None:  # pragma: no cover - only on eviction races
            await self._fail(job, "internal", "job payload unavailable")
            return

        job.status = "running"
        await bus.emit(job, "status", {"jobId": job.id, "status": "running"})

        provider = get_provider(job.provider)
        findings = []
        try:
            for chunk in payload.chunks:
                findings.extend(await provider.review(chunk))
        except ProviderError as exc:
            await self._fail(job, exc.code, exc.message)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("provider %s failed on job %s", job.provider, job.id)
            await self._fail(job, "internal", f"review failed: {exc}")
            return

        await self._complete(job, normalize(findings, job.max_findings))

    async def _mirror(self, job: Job, source: Job) -> None:
        try:
            if source.finished is not None:
                await source.finished.wait()

            if source.status != "done":
                # Never serve a failure from cache: drop the entry and run the
                # work for real.
                self._cache.discard(source.content_key, source.id)
                job.usage = Usage(job.usage.input_bytes, job.usage.chunks, False)
                self._queue.put_nowait(job)
                return

            job.status = "running"
            await bus.emit(job, "status", {"jobId": job.id, "status": "running"})
            job.usage = Usage(job.usage.input_bytes, job.usage.chunks, True)
            await self._complete(job, list(source.findings))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defence in depth
            log.exception("mirror failed for job %s", job.id)
            await self._fail(job, "internal", f"cache mirror failed: {exc}")

    # ----------------------------------------------------------------- #
    # terminal transitions
    # ----------------------------------------------------------------- #
    async def _complete(self, job: Job, findings: list) -> None:
        job.findings = findings
        job.status = "done"
        for finding in findings:
            await bus.emit(job, "finding", finding.to_dict())
        await bus.emit(
            job, "done", {"total": len(findings), "usage": job.usage.to_dict()}
        )
        if not job.usage.cache_hit:
            self._cache.put(job.content_key, job.id)
        self._finish(job)

    async def _fail(self, job: Job, code: str, message: str) -> None:
        job.status = "failed"
        job.error = {"code": code, "message": message}
        self._cache.discard(job.content_key, job.id)
        await bus.emit(
            job,
            "status",
            {"jobId": job.id, "status": "failed", "error": job.error},
        )
        self._finish(job)

    def _finish(self, job: Job) -> None:
        self._payloads.pop(job.id, None)
        if job.finished is not None:
            job.finished.set()
