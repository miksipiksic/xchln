from __future__ import annotations

import asyncio

from app.config import MAX_CONCURRENT_JOBS
from app.providers.base import Provider
from tests import diffs
from tests.conftest import AUTH, submit, wait_for


class TrackingProvider(Provider):
    """Records how many reviews overlap, so the pool size can be observed."""

    name = "tracking"

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def review(self, chunk):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            return []
        finally:
            self.active -= 1


async def test_four_jobs_run_concurrently_and_a_fifth_queues(client, monkeypatch):
    import app.jobs.queue as queue_module

    provider = TrackingProvider()
    monkeypatch.setattr(queue_module, "get_provider", lambda name: provider)

    # Distinct payloads, so nothing is served from cache.
    ids = [
        await submit(client, diffs.large_multifile_diff(file_count=2, lines_per_file=n))
        for n in range(1, MAX_CONCURRENT_JOBS + 2)
    ]

    results = await asyncio.gather(*(wait_for(client, job_id) for job_id in ids))

    assert len(ids) == MAX_CONCURRENT_JOBS + 1
    assert all(r["status"] == "done" for r in results)   # the 5th queued, never failed
    assert provider.peak == MAX_CONCURRENT_JOBS          # and no more than four at once


async def test_many_jobs_all_complete(client, monkeypatch):
    import app.jobs.queue as queue_module

    provider = TrackingProvider(delay=0.05)
    monkeypatch.setattr(queue_module, "get_provider", lambda name: provider)

    ids = [
        await submit(client, diffs.large_multifile_diff(file_count=1, lines_per_file=n))
        for n in range(1, 13)
    ]
    results = await asyncio.gather(*(wait_for(client, job_id) for job_id in ids))

    assert all(r["status"] == "done" for r in results)
    assert provider.peak <= MAX_CONCURRENT_JOBS


async def test_polling_stays_responsive_while_jobs_run(client, monkeypatch):
    """Health must answer while the pool is saturated - the CPU-bound mock
    scan is offloaded to threads precisely so the loop stays free."""
    import app.jobs.queue as queue_module

    provider = TrackingProvider(delay=0.3)
    monkeypatch.setattr(queue_module, "get_provider", lambda name: provider)

    for n in range(1, MAX_CONCURRENT_JOBS + 1):
        await submit(client, diffs.large_multifile_diff(file_count=1, lines_per_file=n))

    for _ in range(10):
        assert (await client.get("/health")).status_code == 200


async def test_real_mock_provider_meets_the_latency_budget(client):
    """A 64 KiB diff must reach done well inside the 30 s allowance."""
    diff = diffs.large_multifile_diff(file_count=30, lines_per_file=20)
    assert len(diff.encode("utf-8")) <= 65_536

    started = asyncio.get_running_loop().time()
    body = await wait_for(client, await submit(client, diff), timeout=30.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert body["status"] == "done"
    assert elapsed < 5.0
