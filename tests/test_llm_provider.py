from __future__ import annotations

import json

import httpx
import pytest

from app.diff.chunker import chunk_files
from app.diff.parser import parse_diff
from app.providers.base import ProviderError
from app.providers.llm import LlmProvider, _anchor, _batches, _build_index, _render
from tests import diffs
from tests.conftest import AUTH, submit, wait_for
from tests.test_sse import parse_sse, read_stream


def one_chunk(diff: str):
    return chunk_files(parse_diff(diff))[0]


def completion(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
    )


def provider_with(handler) -> LlmProvider:
    provider = LlmProvider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")


# --------------------------------------------------------------------------- #
# Re-anchoring: the structural defence against hallucination and injection
# --------------------------------------------------------------------------- #
def test_findings_are_anchored_to_real_added_lines():
    chunk = one_chunk(diffs.NEW_FILE)
    index = _build_index(chunk)

    findings = _anchor(
        json.dumps(
            {
                "findings": [
                    {"path": "added.js", "line": 1, "severity": "high",
                     "category": "security", "title": "real"},
                    # A file that was never sent.
                    {"path": "/etc/passwd", "line": 1, "severity": "critical",
                     "category": "security", "title": "invented file"},
                    # A line that is not an added line in this chunk.
                    {"path": "added.js", "line": 9999, "severity": "critical",
                     "category": "security", "title": "invented line"},
                ]
            }
        ),
        index,
    )

    assert [(f.path, f.line, f.title) for f in findings] == [("added.js", 1, "real")]


def test_evidence_comes_from_our_parse_not_the_model():
    chunk = one_chunk(diffs.NEW_FILE)
    index = _build_index(chunk)
    findings = _anchor(
        json.dumps(
            {
                "findings": [
                    {"path": "added.js", "line": 1, "severity": "low",
                     "category": "style", "title": "t",
                     "evidence": "<script>alert(1)</script>"}
                ]
            }
        ),
        index,
    )
    assert findings[0].evidence == 'console.log("brand new");'


def test_out_of_range_enums_are_coerced():
    chunk = one_chunk(diffs.NEW_FILE)
    index = _build_index(chunk)
    findings = _anchor(
        json.dumps(
            {
                "findings": [
                    {"path": "added.js", "line": 1, "severity": "APOCALYPTIC",
                     "category": "vibes", "title": "x" * 500}
                ]
            }
        ),
        index,
    )
    assert findings[0].severity == "low"
    assert findings[0].category == "correctness"
    assert len(findings[0].title) <= 80


def test_malformed_model_output_is_a_provider_error():
    index = _build_index(one_chunk(diffs.NEW_FILE))
    with pytest.raises(ProviderError):
        _anchor("not json at all", index)
    with pytest.raises(ProviderError):
        _anchor(json.dumps({"result": "wrong shape"}), index)


def test_prompt_fences_untrusted_content():
    prompt = _render(_build_index(one_chunk(diffs.INJECTION)))
    assert "untrusted" in prompt.lower()
    # The fence carries a nonce, so diff content cannot forge the terminator.
    assert "UNTRUSTED-DIFF-" in prompt
    assert "Ignore previous instructions" in prompt      # present, but as data


# --------------------------------------------------------------------------- #
# Transport behaviour
# --------------------------------------------------------------------------- #
async def test_successful_call_returns_anchored_findings():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return completion(
            {"findings": [{"path": "added.js", "line": 2, "severity": "medium",
                           "category": "performance", "title": "slow"}]}
        )

    findings = await provider_with(handler).review(one_chunk(diffs.NEW_FILE))
    assert len(findings) == 1
    assert findings[0].rule_id == "LLM-PERF"
    assert findings[0].id == "LLM-PERF:added.js:2"


async def test_http_error_becomes_a_provider_error():
    provider = provider_with(lambda request: httpx.Response(401, json={"error": "nope"}))
    with pytest.raises(ProviderError) as exc:
        await provider.review(one_chunk(diffs.NEW_FILE))
    assert "401" in str(exc.value)


async def test_transport_failure_is_retried_then_reported():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("no route to host")

    with pytest.raises(ProviderError):
        await provider_with(handler).review(one_chunk(diffs.NEW_FILE))
    assert calls["n"] == 2      # one retry, then give up cleanly


async def test_missing_credentials_is_reported_not_crashed(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ProviderError) as exc:
        await LlmProvider().review(one_chunk(diffs.NEW_FILE))
    assert "GROQ_API_KEY" in str(exc.value)


# --------------------------------------------------------------------------- #
# End to end: an unreachable model fails the job, never the service
# --------------------------------------------------------------------------- #
async def test_unconfigured_llm_job_fails_gracefully(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    job_id = await submit(client, diffs.ALL_RULES, options={"provider": "llm"})
    body = await wait_for(client, job_id)

    assert body["status"] == "failed"
    assert body["error"]["message"]
    assert "findings" not in body

    # The service is entirely unharmed.
    assert (await client.get("/health")).json()["status"] == "ok"
    mock_job = await wait_for(client, await submit(client, diffs.ALL_RULES))
    assert mock_job["status"] == "done"


async def test_failed_job_stream_terminates_with_a_status_event(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    job_id = await submit(client, diffs.ALL_RULES, options={"provider": "llm"})
    await wait_for(client, job_id)

    events = parse_sse(await read_stream(client, job_id))
    kinds = [kind for _, kind, _ in events]
    assert kinds[-1] == "status"
    assert events[-1][2]["status"] == "failed"
    assert "done" not in kinds


async def test_a_failure_is_never_served_from_cache(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    first = await wait_for(
        client, await submit(client, diffs.NEW_FILE, options={"provider": "llm"})
    )
    assert first["status"] == "failed"

    second = await wait_for(
        client, await submit(client, diffs.NEW_FILE, options={"provider": "llm"})
    )
    assert second["status"] == "failed"
    assert second["usage"]["cacheHit"] is False


# --------------------------------------------------------------------------- #
# Batching: a 64 KiB chunk does not fit in one model call
# --------------------------------------------------------------------------- #
def test_batches_stay_under_the_request_budget():
    index = _build_index(one_chunk(diffs.large_multifile_diff(file_count=3, lines_per_file=200)))
    batches = list(_batches(index, 4000))

    assert len(batches) > 1
    for batch in batches:
        rendered = sum(len(t) for lines in batch.values() for t in lines.values())
        assert rendered <= 4000 * 1.5      # budget plus per-line overhead


def test_batching_loses_no_lines_and_invents_none():
    index = _build_index(one_chunk(diffs.large_multifile_diff(file_count=3, lines_per_file=200)))
    original = {(p, n) for p, lines in index.items() for n in lines}

    seen: set[tuple[str, int]] = set()
    for batch in _batches(index, 4000):
        for path, lines in batch.items():
            for number, text in lines.items():
                assert index[path][number] == text
                assert (path, number) not in seen      # no duplication
                seen.add((path, number))

    assert seen == original                            # no loss


async def test_a_large_chunk_becomes_several_calls():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return completion({"findings": []})

    big = one_chunk(diffs.large_multifile_diff(file_count=4, lines_per_file=200))
    await provider_with(handler).review(big)
    assert calls["n"] > 1


def test_retry_delay_honours_and_caps_retry_after():
    from app.providers.llm import MAX_RETRY_DELAY, _retry_delay

    assert _retry_delay("2") == 2.0
    assert _retry_delay("900") == MAX_RETRY_DELAY      # never wait out a quota window
    assert _retry_delay(None) == 1.0
    assert _retry_delay("not-a-number") == 1.0


async def test_rate_limited_call_waits_before_retrying(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.providers.llm.asyncio.sleep", fake_sleep)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={})
        return completion({"findings": []})

    await provider_with(handler).review(one_chunk(diffs.NEW_FILE))
    assert attempts["n"] == 2
    assert slept == [2.0]                              # waited, rather than hammering
