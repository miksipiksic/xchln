from __future__ import annotations

import asyncio
import json

from app.models import Finding
from tests import diffs
from tests.conftest import AUTH, submit, wait_for


def parse_sse(raw: str) -> list[tuple[int, str, dict]]:
    """Parse the wire format back into (id, event, data) triples."""
    events = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        fields = {}
        for line in block.split("\n"):
            key, _, value = line.partition(": ")
            fields[key] = value
        events.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return events


async def read_stream(client, job_id: str, headers: dict | None = None) -> str:
    async with client.stream(
        "GET", f"/v1/reviews/{job_id}/stream", headers={**AUTH, **(headers or {})}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        return (await response.aread()).decode("utf-8")


# --------------------------------------------------------------------------- #
async def test_stream_of_a_finished_job_carries_the_whole_lifecycle(client):
    job_id = await submit(client, diffs.ALL_RULES)
    result = await wait_for(client, job_id)

    events = parse_sse(await read_stream(client, job_id))
    types = [kind for _, kind, _ in events]

    assert types[0] == "status" and events[0][2]["status"] == "queued"
    assert "running" in [d.get("status") for _, k, d in events if k == "status"]
    assert types[-1] == "done"

    findings = [data for _, kind, data in events if kind == "finding"]
    assert findings == result["findings"]

    done = events[-1][2]
    assert done["total"] == len(result["findings"])
    assert done["usage"] == result["usage"]


async def test_event_ids_are_sequential(client):
    job_id = await submit(client, diffs.ALL_RULES)
    await wait_for(client, job_id)
    events = parse_sse(await read_stream(client, job_id))
    assert [seq for seq, _, _ in events] == list(range(1, len(events) + 1))


async def test_replay_is_byte_identical(client):
    """The scored property: reconnecting to a finished job replays exactly."""
    job_id = await submit(client, diffs.ALL_RULES)
    await wait_for(client, job_id)

    first = await read_stream(client, job_id)
    second = await read_stream(client, job_id)
    third = await read_stream(client, job_id)

    assert first == second == third
    assert first.count("event: done") == 1
    assert ": keep-alive" not in first          # no heartbeats in a replay


async def test_stream_ordering_matches_result_ordering(client):
    job_id = await submit(client, diffs.large_multifile_diff())
    result = await wait_for(client, job_id)

    events = parse_sse(await read_stream(client, job_id))
    streamed = [data for _, kind, data in events if kind == "finding"]

    assert streamed == result["findings"]
    keys = [(f["path"], f["line"], f["ruleId"]) for f in streamed]
    assert keys == sorted(keys)


async def test_last_event_id_resumes_without_repeating(client):
    job_id = await submit(client, diffs.ALL_RULES)
    await wait_for(client, job_id)

    full = parse_sse(await read_stream(client, job_id))
    resumed = parse_sse(
        await read_stream(client, job_id, {"Last-Event-ID": str(full[2][0])})
    )

    assert resumed == full[3:]


async def test_live_stream_delivers_events_as_they_happen(client, monkeypatch):
    """Attach before the job finishes and watch the transitions arrive."""
    import app.jobs.queue as queue_module
    from app.providers.base import Provider

    class SlowProvider(Provider):
        name = "slow"

        async def review(self, chunk):
            await asyncio.sleep(0.25)
            return [
                Finding("MOCK-007", "slow.js", 1, "low", "style", "console.log left in", "x")
            ]

    monkeypatch.setattr(queue_module, "get_provider", lambda name: SlowProvider())

    job_id = await submit(client, diffs.NEW_FILE)
    raw = await read_stream(client, job_id)          # connects while queued/running
    events = parse_sse(raw)

    statuses = [d["status"] for _, k, d in events if k == "status"]
    assert statuses == ["queued", "running"]
    assert [k for _, k, _ in events][-1] == "done"
    assert len([k for _, k, _ in events if k == "finding"]) == 1


async def test_two_concurrent_listeners_see_the_same_stream(client, monkeypatch):
    import app.jobs.queue as queue_module
    from app.providers.base import Provider

    class SlowProvider(Provider):
        name = "slow"

        async def review(self, chunk):
            await asyncio.sleep(0.25)
            return []

    monkeypatch.setattr(queue_module, "get_provider", lambda name: SlowProvider())

    job_id = await submit(client, diffs.NEW_FILE)
    first, second = await asyncio.gather(
        read_stream(client, job_id), read_stream(client, job_id)
    )
    assert parse_sse(first) == parse_sse(second)

    # And a listener arriving after the fact gets the identical replay.
    assert parse_sse(await read_stream(client, job_id)) == parse_sse(first)


async def test_stream_of_unknown_job_is_a_json_404(client):
    response = await client.get("/v1/reviews/nope/stream", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
