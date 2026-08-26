from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import create_app

TOKEN = "test-token-abc123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client(monkeypatch):
    """An in-process client over the real ASGI app, lifespan included.

    Each test gets a fresh app, so the job store, content cache, idempotency
    index and rate-limit buckets are all isolated.
    """
    monkeypatch.setenv("API_BEARER_TOKEN", TOKEN)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


async def submit(client: httpx.AsyncClient, diff: str, **kwargs) -> str:
    body: dict = {"diff": diff}
    options = kwargs.pop("options", None)
    if options is not None:
        body["options"] = options
    headers = dict(AUTH)
    headers.update(kwargs.pop("headers", {}))
    response = await client.post("/v1/reviews", json=body, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["jobId"]


async def wait_for(client: httpx.AsyncClient, job_id: str, timeout: float = 30.0) -> dict:
    """Poll until terminal. The contract allows 30 s for a diff under 64 KiB."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/reviews/{job_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


async def review(client: httpx.AsyncClient, diff: str, **kwargs) -> dict:
    return await wait_for(client, await submit(client, diff, **kwargs))
