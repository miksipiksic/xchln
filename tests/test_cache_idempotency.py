from __future__ import annotations

import asyncio

from tests import diffs
from tests.conftest import AUTH, review, submit, wait_for


# --------------------------------------------------------------------------- #
# Caching: keyed by content, no header involved
# --------------------------------------------------------------------------- #
async def test_identical_resubmission_is_a_cache_hit(client):
    first = await review(client, diffs.ALL_RULES)
    second = await review(client, diffs.ALL_RULES)

    assert first["usage"]["cacheHit"] is False
    assert second["usage"]["cacheHit"] is True
    assert second["findings"] == first["findings"]
    assert second["usage"]["chunks"] == first["usage"]["chunks"]
    assert second["usage"]["inputBytes"] == first["usage"]["inputBytes"]


async def test_a_cache_hit_gets_its_own_job_id(client):
    """Reusing the first id would retroactively flip its own cacheHit to true."""
    first_id = await submit(client, diffs.ALL_RULES)
    await wait_for(client, first_id)
    second_id = await submit(client, diffs.ALL_RULES)

    assert second_id != first_id
    original = await client.get(f"/v1/reviews/{first_id}", headers=AUTH)
    assert original.json()["usage"]["cacheHit"] is False


async def test_cache_hit_applies_with_or_without_an_idempotency_key(client):
    await wait_for(client, await submit(client, diffs.ALL_RULES))
    keyed = await wait_for(
        client,
        await submit(client, diffs.ALL_RULES, headers={"Idempotency-Key": "fresh-key"}),
    )
    assert keyed["usage"]["cacheHit"] is True


async def test_options_are_part_of_the_cache_key(client):
    await review(client, diffs.ALL_RULES, options={"maxFindings": 100})
    other = await review(client, diffs.ALL_RULES, options={"maxFindings": 2})
    assert other["usage"]["cacheHit"] is False
    assert len(other["findings"]) == 2


async def test_omitted_options_match_explicit_defaults(client):
    await review(client, diffs.ALL_RULES)
    explicit = await review(
        client, diffs.ALL_RULES, options={"provider": "mock", "maxFindings": 100}
    )
    assert explicit["usage"]["cacheHit"] is True


async def test_different_diffs_are_not_confused(client):
    first = await review(client, diffs.ALL_RULES)
    second = await review(client, diffs.NEW_FILE)
    assert second["usage"]["cacheHit"] is False
    assert second["findings"] != first["findings"]


async def test_concurrent_identical_submissions_do_not_duplicate_work(client):
    """The content key is registered before the scan starts, so the second
    submission mirrors the first rather than repeating it."""
    ids = await asyncio.gather(
        *(submit(client, diffs.large_multifile_diff()) for _ in range(4))
    )
    results = [await wait_for(client, job_id) for job_id in ids]

    assert len(set(ids)) == 4
    assert sum(1 for r in results if r["usage"]["cacheHit"]) >= 1
    findings = [r["findings"] for r in results]
    assert all(f == findings[0] for f in findings)


# --------------------------------------------------------------------------- #
# Idempotency: keyed by header + body
# --------------------------------------------------------------------------- #
async def test_same_key_and_body_returns_the_same_job(client):
    headers = {**AUTH, "Idempotency-Key": "abc-123"}
    body = {"diff": diffs.ALL_RULES}

    first = await client.post("/v1/reviews", json=body, headers=headers)
    second = await client.post("/v1/reviews", json=body, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["jobId"] == second.json()["jobId"]
    assert second.json()["status"] == "queued"


async def test_same_key_after_completion_still_returns_the_same_job(client):
    headers = {**AUTH, "Idempotency-Key": "abc-456"}
    body = {"diff": diffs.ALL_RULES}

    first = await client.post("/v1/reviews", json=body, headers=headers)
    job_id = first.json()["jobId"]
    await wait_for(client, job_id)

    replay = await client.post("/v1/reviews", json=body, headers=headers)
    assert replay.json()["jobId"] == job_id


async def test_same_key_different_body_is_409(client):
    headers = {**AUTH, "Idempotency-Key": "abc-789"}
    await client.post("/v1/reviews", json={"diff": diffs.ALL_RULES}, headers=headers)
    conflict = await client.post(
        "/v1/reviews", json={"diff": diffs.NEW_FILE}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_conflict_detection_is_byte_sensitive(client):
    """Same diff, different option value: still a different body."""
    headers = {**AUTH, "Idempotency-Key": "abc-999"}
    await client.post("/v1/reviews", json={"diff": diffs.NEW_FILE}, headers=headers)
    conflict = await client.post(
        "/v1/reviews",
        json={"diff": diffs.NEW_FILE, "options": {"maxFindings": 5}},
        headers=headers,
    )
    assert conflict.status_code == 409


async def test_different_keys_same_body_are_separate_jobs(client):
    body = {"diff": diffs.ALL_RULES}
    first = await client.post(
        "/v1/reviews", json=body, headers={**AUTH, "Idempotency-Key": "k1"}
    )
    second = await client.post(
        "/v1/reviews", json=body, headers={**AUTH, "Idempotency-Key": "k2"}
    )
    assert first.json()["jobId"] != second.json()["jobId"]
    # ...but the second still gets its answer from cache rather than rescanning.
    assert (await wait_for(client, second.json()["jobId"]))["usage"]["cacheHit"] is True
