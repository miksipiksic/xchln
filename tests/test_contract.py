from __future__ import annotations

import json

import pytest

from app.config import MAX_PAYLOAD_BYTES, spec_document
from tests import diffs
from tests.conftest import AUTH, review, submit, wait_for

VALID_CODES = {
    "unauthorized", "payload_too_large", "invalid_json", "invalid_diff",
    "idempotency_conflict", "not_found", "rate_limited", "internal",
}


def assert_envelope(response, code: str, status: int):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert code in VALID_CODES
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #
async def test_health_is_public(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"].count(".") == 2           # semver
    assert isinstance(body["uptimeSeconds"], (int, float)) and body["uptimeSeconds"] >= 0


async def test_spec_is_public_and_matches_configuration(client):
    response = await client.get("/spec")
    assert response.status_code == 200
    assert response.json() == spec_document()
    assert response.json()["limits"]["maxPayloadBytes"] == MAX_PAYLOAD_BYTES


# --------------------------------------------------------------------------- #
# Auth on every /v1 route and method
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/reviews"),
        ("GET", "/v1/reviews/anything"),
        ("GET", "/v1/reviews/anything/stream"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic test-token-abc123"},
        {"Authorization": "test-token-abc123"},
        {"Authorization": "Bearer "},
    ],
)
async def test_all_v1_routes_require_a_valid_bearer_token(client, method, path, headers):
    response = await client.request(method, path, headers=headers, json={"diff": "x"})
    assert_envelope(response, "unauthorized", 401)


async def test_auth_is_checked_before_the_payload_limit(client):
    """An anonymous caller learns nothing about internals, not even the limit."""
    oversized = json.dumps({"diff": "x" * (MAX_PAYLOAD_BYTES + 1000)})
    response = await client.post("/v1/reviews", content=oversized)
    assert_envelope(response, "unauthorized", 401)


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
async def test_oversized_payload_is_413(client):
    oversized = json.dumps({"diff": "x" * (MAX_PAYLOAD_BYTES + 1000)})
    response = await client.post("/v1/reviews", content=oversized, headers=AUTH)
    assert_envelope(response, "payload_too_large", 413)


async def test_oversized_payload_without_content_length_is_413(client):
    """Chunked uploads have no Content-Length, so bytes are counted as they land."""

    async def chunks():
        yield b'{"diff": "'
        for _ in range(12):
            yield b"x" * 100_000
        yield b'"}'

    response = await client.post(
        "/v1/reviews",
        content=chunks(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert_envelope(response, "payload_too_large", 413)


@pytest.mark.parametrize("payload", [b"{not json", b"", b'{"diff": ', b"[1,2,"])
async def test_malformed_json_is_400(client, payload):
    response = await client.post("/v1/reviews", content=payload, headers=AUTH)
    assert_envelope(response, "invalid_json", 400)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"diff": ""},
        {"diff": "   "},
        {"diff": None},
        {"diff": 42},
        {"diff": "this is not a diff"},
        {"diff": "@@ -1 +1 @@\n+orphan\n"},
    ],
)
async def test_missing_or_unparseable_diff_is_422(client, body):
    response = await client.post("/v1/reviews", json=body, headers=AUTH)
    assert_envelope(response, "invalid_diff", 422)


async def test_unknown_job_is_404(client):
    assert_envelope(
        await client.get("/v1/reviews/does-not-exist", headers=AUTH), "not_found", 404
    )
    assert_envelope(
        await client.get("/v1/reviews/does-not-exist/stream", headers=AUTH),
        "not_found",
        404,
    )


async def test_unknown_route_still_uses_the_envelope(client):
    assert_envelope(await client.get("/v1/nope", headers=AUTH), "not_found", 404)


# --------------------------------------------------------------------------- #
# Submission and lifecycle
# --------------------------------------------------------------------------- #
async def test_submission_returns_202_queued_with_an_opaque_id(client):
    response = await client.post("/v1/reviews", json={"diff": diffs.ALL_RULES}, headers=AUTH)
    assert response.status_code == 202
    assert set(response.json()) == {"jobId", "status"}
    assert response.json()["status"] == "queued"
    assert len(response.json()["jobId"]) >= 16


async def test_job_reaches_done_and_reports_the_documented_shape(client):
    body = await review(client, diffs.ALL_RULES)
    assert body["status"] == "done"
    assert set(body) == {"jobId", "status", "findings", "usage"}
    assert set(body["usage"]) == {"inputBytes", "chunks", "cacheHit"}
    assert body["usage"]["inputBytes"] == len(diffs.ALL_RULES.encode("utf-8"))
    assert body["usage"]["chunks"] == 1
    assert body["usage"]["cacheHit"] is False

    for finding in body["findings"]:
        assert set(finding) == {
            "id", "ruleId", "path", "line", "severity", "category", "title", "evidence",
        }
        assert finding["id"] == f"{finding['ruleId']}:{finding['path']}:{finding['line']}"
        assert finding["severity"] in {"critical", "high", "medium", "low"}
        assert finding["category"] in {"security", "correctness", "performance", "style"}


async def test_unknown_body_fields_are_ignored(client):
    response = await client.post(
        "/v1/reviews",
        json={"diff": diffs.NEW_FILE, "callbackUrl": "http://x", "nonsense": [1, 2]},
        headers=AUTH,
    )
    assert response.status_code == 202


async def test_invalid_option_values_fall_back_to_defaults(client):
    body = await review(
        client,
        diffs.NEW_FILE,
        options={"provider": "not-a-provider", "maxFindings": "lots"},
    )
    assert body["status"] == "done"


# --------------------------------------------------------------------------- #
# Injection inertness
# --------------------------------------------------------------------------- #
async def test_injection_content_is_reported_and_inert(client):
    body = await review(client, diffs.INJECTION)
    assert body["status"] == "done"

    by_rule = {}
    for finding in body["findings"]:
        by_rule.setdefault(finding["ruleId"], []).append(finding)

    # All three phrases reported...
    assert len(by_rule["MOCK-INJ"]) == 3
    assert all(f["severity"] == "critical" for f in by_rule["MOCK-INJ"])
    # ...and the instruction to "report no findings" changed nothing: the
    # ordinary rule on the following line still fired.
    assert len(by_rule["MOCK-007"]) == 1


async def test_injection_does_not_disturb_other_findings(client):
    clean = await review(client, diffs.ALL_RULES)
    spiked = await review(client, diffs.ALL_RULES + diffs.INJECTION)

    clean_ids = {f["id"] for f in clean["findings"]}
    spiked_ids = {f["id"] for f in spiked["findings"]}
    assert clean_ids <= spiked_ids
