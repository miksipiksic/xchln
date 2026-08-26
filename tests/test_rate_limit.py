from __future__ import annotations

from app.config import RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE
from app.deps.rate_limit import TokenBucket
from tests import diffs
from tests.conftest import AUTH


async def test_sustained_declared_rate_never_sheds():
    """30/min for ten minutes, paced evenly - not one rejection.

    Driven against the bucket with a simulated clock rather than over HTTP, so
    the property is proven for ten minutes of traffic without a ten-minute test.
    """
    bucket = TokenBucket(RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE)
    interval = 60.0 / RATE_LIMIT_PER_MINUTE

    for _ in range(RATE_LIMIT_PER_MINUTE * 10):
        allowed, _ = bucket.take()
        assert allowed is True
        bucket.updated -= interval          # advance the simulated clock


async def test_burst_beyond_capacity_sheds_then_recovers():
    bucket = TokenBucket(RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE)
    results = [bucket.take()[0] for _ in range(RATE_LIMIT_BURST + 20)]

    assert results[:RATE_LIMIT_BURST] == [True] * RATE_LIMIT_BURST
    assert results[RATE_LIMIT_BURST:] == [False] * 20

    bucket.updated -= 60.0                  # a minute passes
    assert bucket.take()[0] is True


async def test_burst_over_http_returns_429_with_retry_after(client):
    body = {"diff": diffs.NEW_FILE}
    responses = []
    for _ in range(RATE_LIMIT_BURST + 10):
        responses.append(await client.post("/v1/reviews", json=body, headers=AUTH))

    statuses = [r.status_code for r in responses]
    assert statuses.count(202) == RATE_LIMIT_BURST
    limited = [r for r in responses if r.status_code == 429]
    assert len(limited) == 10

    for response in limited:
        assert response.json()["error"]["code"] == "rate_limited"
        assert int(response.headers["Retry-After"]) >= 1

    # Never a 5xx under burst.
    assert not any(status >= 500 for status in statuses)


async def test_gets_are_never_rate_limited(client):
    first = await client.post("/v1/reviews", json={"diff": diffs.NEW_FILE}, headers=AUTH)
    job_id = first.json()["jobId"]

    # Exhaust the POST budget.
    for _ in range(RATE_LIMIT_BURST + 5):
        await client.post("/v1/reviews", json={"diff": diffs.ALL_RULES}, headers=AUTH)

    for _ in range(50):
        response = await client.get(f"/v1/reviews/{job_id}", headers=AUTH)
        assert response.status_code == 200

    stream = await client.get(f"/v1/reviews/{job_id}/stream", headers=AUTH)
    assert stream.status_code == 200


async def test_public_routes_are_never_rate_limited(client):
    for _ in range(RATE_LIMIT_BURST + 10):
        await client.post("/v1/reviews", json={"diff": diffs.NEW_FILE}, headers=AUTH)

    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/spec")).status_code == 200
