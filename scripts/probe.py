"""Contract probe against a *running* service, over the network.

The pytest suite proves the same behaviours in-process; this proves them
through a real socket, a real proxy and a real deployment, which is the only
way to catch the things that only break in production: a proxy buffering SSE,
a platform capping request bodies, a cold start blowing the latency budget.

    python scripts/probe.py https://your-host $BEARER_TOKEN

Exits non-zero if anything fails. Diff fixtures are imported from tests/ so the
two suites can never drift apart.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from tests import diffs  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    marker = "ok  " if ok else "FAIL"
    print(f"  [{marker}] {name}" + (f" - {detail}" if detail and not ok else ""))
    return ok


class Probe:
    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        self.auth = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(timeout=40.0, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def post(self, body, **kwargs):
        headers = {**self.auth, **kwargs.pop("headers", {})}
        return await self.client.post(f"{self.base}/v1/reviews", headers=headers, **({"json": body} if isinstance(body, (dict, list)) else {"content": body}), **kwargs)

    async def submit(self, diff: str, options: dict | None = None, headers: dict | None = None) -> str:
        body: dict = {"diff": diff}
        if options:
            body["options"] = options
        response = await self.post(body, headers=headers or {})
        assert response.status_code == 202, f"submit failed: {response.status_code} {response.text}"
        return response.json()["jobId"]

    async def wait(self, job_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await self.client.get(f"{self.base}/v1/reviews/{job_id}", headers=self.auth)
            body = response.json()
            if body["status"] in ("done", "failed"):
                return body
            await asyncio.sleep(0.2)
        raise TimeoutError(f"job {job_id} still {body['status']} after {timeout}s")

    async def stream(self, job_id: str, headers: dict | None = None) -> str:
        async with self.client.stream(
            "GET",
            f"{self.base}/v1/reviews/{job_id}/stream",
            headers={**self.auth, **(headers or {})},
        ) as response:
            assert response.status_code == 200, response.status_code
            self.last_stream_headers = response.headers
            return (await response.aread()).decode("utf-8")


def unique_diff(tag: str) -> str:
    """A valid diff nobody has submitted before.

    The cache checks must start from a genuine miss, so they cannot reuse a
    fixture that an earlier probe run against the same server already cached.
    """
    lines = [
        f"diff --git a/probe/{tag}.js b/probe/{tag}.js",
        f"--- a/probe/{tag}.js",
        f"+++ b/probe/{tag}.js",
        "@@ -1,1 +1,3 @@",
        " const head = 1;",
        f'+console.log("probe {tag}");',
        "+// TODO probe",
    ]
    return "\n".join(lines) + "\n"


def sse_events(raw: str) -> list[tuple[int, str, dict]]:
    events = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        fields = {}
        for line in block.split("\n"):
            key, _, value = line.partition(": ")
            fields[key] = value
        if "event" in fields:
            events.append((int(fields.get("id", 0)), fields["event"], json.loads(fields["data"])))
    return events


# --------------------------------------------------------------------------- #
async def check_public(p: Probe) -> None:
    print("\npublic routes")
    health = await p.client.get(f"{p.base}/health")
    body = health.json()
    record("GET /health is public and 200", health.status_code == 200)
    record(
        "health shape {status, version, uptimeSeconds}",
        body.get("status") == "ok"
        and isinstance(body.get("version"), str)
        and body["version"].count(".") == 2
        and isinstance(body.get("uptimeSeconds"), (int, float)),
        json.dumps(body),
    )

    spec = await p.client.get(f"{p.base}/spec")
    s = spec.json()
    record("GET /spec is public and 200", spec.status_code == 200)
    record(
        "spec declares specVersion, providers, limits",
        s.get("specVersion") == "1.0"
        and set(s.get("providers", [])) >= {"mock", "llm"}
        and set(s.get("limits", {}))
        == {"maxPayloadBytes", "chunkBytes", "maxConcurrentJobs", "rateLimitPerMinute"},
        json.dumps(s),
    )
    p.limits = s["limits"]


async def check_auth(p: Probe) -> None:
    print("\nauthentication")
    for method, path in (
        ("POST", "/v1/reviews"),
        ("GET", "/v1/reviews/whatever"),
        ("GET", "/v1/reviews/whatever/stream"),
    ):
        for label, headers in (
            ("no header", {}),
            ("wrong token", {"Authorization": "Bearer definitely-wrong"}),
            ("wrong scheme", {"Authorization": "Basic abc"}),
        ):
            response = await p.client.request(
                method, f"{p.base}{path}", headers=headers, json={"diff": "x"}
            )
            envelope = response.json().get("error", {})
            record(
                f"{method} {path} [{label}] -> 401 unauthorized",
                response.status_code == 401 and envelope.get("code") == "unauthorized",
                f"{response.status_code} {response.text[:80]}",
            )


async def check_errors(p: Probe) -> None:
    print("\nerror taxonomy")
    limit = p.limits["maxPayloadBytes"]

    oversized = json.dumps({"diff": "x" * (limit + 2048)})
    response = await p.post(oversized.encode())
    record(
        "oversized body -> 413 payload_too_large",
        response.status_code == 413
        and response.json().get("error", {}).get("code") == "payload_too_large",
        f"{response.status_code} {response.text[:80]}",
    )

    response = await p.post(b"{not json")
    record(
        "malformed JSON -> 400 invalid_json",
        response.status_code == 400
        and response.json().get("error", {}).get("code") == "invalid_json",
        f"{response.status_code} {response.text[:80]}",
    )

    for label, body in (
        ("missing diff", {}),
        ("empty diff", {"diff": ""}),
        ("prose, not a diff", {"diff": "hello world"}),
    ):
        response = await p.post(body)
        record(
            f"{label} -> 422 invalid_diff",
            response.status_code == 422
            and response.json().get("error", {}).get("code") == "invalid_diff",
            f"{response.status_code} {response.text[:80]}",
        )

    response = await p.client.get(f"{p.base}/v1/reviews/no-such-job", headers=p.auth)
    record(
        "unknown jobId -> 404 not_found",
        response.status_code == 404
        and response.json().get("error", {}).get("code") == "not_found",
        f"{response.status_code} {response.text[:80]}",
    )


async def check_mock_findings(p: Probe) -> None:
    print("\nmock provider findings")
    started = time.monotonic()
    result = await p.wait(await p.submit(diffs.ALL_RULES))
    elapsed = time.monotonic() - started

    record("job reaches done", result["status"] == "done", json.dumps(result)[:200])
    record("latency budget (<30s)", elapsed < 30.0, f"{elapsed:.1f}s")

    rules = sorted({f["ruleId"] for f in result["findings"]})
    expected = [
        "MOCK-001", "MOCK-002", "MOCK-003", "MOCK-004", "MOCK-005",
        "MOCK-006", "MOCK-007", "MOCK-008", "MOCK-INJ",
    ]
    record("all nine rules fire on the reference diff", rules == expected, str(rules))

    keys = [(f["path"], f["line"], f["ruleId"]) for f in result["findings"]]
    record("findings ordered by path, line, ruleId", keys == sorted(keys))
    ids = [f["id"] for f in result["findings"]]
    record("findings deduplicated by id", len(ids) == len(set(ids)))
    record(
        "finding objects have the documented fields",
        all(
            set(f) == {"id", "ruleId", "path", "line", "severity", "category", "title", "evidence"}
            for f in result["findings"]
        ),
    )
    record(
        "usage has exactly inputBytes, chunks, cacheHit",
        set(result["usage"]) == {"inputBytes", "chunks", "cacheHit"},
        json.dumps(result["usage"]),
    )
    record(
        "inputBytes matches the submitted diff",
        result["usage"]["inputBytes"] == len(diffs.ALL_RULES.encode("utf-8")),
        str(result["usage"]["inputBytes"]),
    )

    capped = await p.wait(await p.submit(diffs.ALL_RULES, {"maxFindings": 3}))
    record("maxFindings truncates the ordered list", len(capped["findings"]) == 3)
    record(
        "maxFindings does not distort usage",
        capped["usage"]["inputBytes"] == result["usage"]["inputBytes"],
    )


async def check_injection(p: Probe) -> None:
    print("\ninjection inertness")
    result = await p.wait(await p.submit(diffs.INJECTION))
    injected = [f for f in result["findings"] if f["ruleId"] == "MOCK-INJ"]
    others = [f for f in result["findings"] if f["ruleId"] == "MOCK-007"]
    record("all three injection phrases reported", len(injected) == 3, str(len(injected)))
    record("injected instruction did not suppress other rules", len(others) == 1)


async def check_chunking(p: Probe) -> None:
    print("\nchunking")
    big = diffs.large_multifile_diff()
    # Ask for the full set: the default cap of 100 would truncate long before
    # the later files, which would make the "nothing lost at a seam" check below
    # vacuous.
    result = await p.wait(await p.submit(big, {"maxFindings": 10000}))

    record("large diff completes", result["status"] == "done")
    record(
        "usage.chunks > 1 for a diff over 64 KiB",
        result["usage"]["chunks"] > 1,
        str(result["usage"]),
    )
    keys = [(f["path"], f["line"], f["ruleId"]) for f in result["findings"]]
    record("ordering preserved across chunk boundaries", keys == sorted(keys))
    ids = [f["id"] for f in result["findings"]]
    record("no duplicates across chunk boundaries", len(ids) == len(set(ids)))

    # Every file in the payload should be represented: nothing lost at a seam.
    paths = {f["path"] for f in result["findings"]}
    record("no findings lost at chunk seams", len(paths) == 40, f"{len(paths)} paths")


async def check_cache_and_idempotency(p: Probe) -> None:
    print("\ncaching and idempotency")
    fresh = unique_diff(f"cache-{int(time.time() * 1000)}")
    first = await p.wait(await p.submit(fresh))
    second = await p.wait(await p.submit(fresh))

    record("first run reports cacheHit false", first["usage"]["cacheHit"] is False)
    record("repeat run reports cacheHit true", second["usage"]["cacheHit"] is True)
    record("cached findings are identical", second["findings"] == first["findings"])

    key = f"probe-{int(time.time() * 1000)}"
    body = {"diff": unique_diff(f"idem-{int(time.time() * 1000)}")}
    one = await p.post(body, headers={"Idempotency-Key": key})
    two = await p.post(body, headers={"Idempotency-Key": key})
    record(
        "same key + same body -> same jobId",
        one.json()["jobId"] == two.json()["jobId"],
        f"{one.json()} vs {two.json()}",
    )

    conflict = await p.post({"diff": diffs.ALL_RULES}, headers={"Idempotency-Key": key})
    record(
        "same key + different body -> 409 idempotency_conflict",
        conflict.status_code == 409
        and conflict.json().get("error", {}).get("code") == "idempotency_conflict",
        f"{conflict.status_code} {conflict.text[:80]}",
    )


async def check_sse(p: Probe) -> None:
    print("\nserver-sent events")
    job_id = await p.submit(diffs.ALL_RULES + diffs.TWO_FILES_UNSORTED)
    result = await p.wait(job_id)

    first = await p.stream(job_id)
    headers = p.last_stream_headers
    record(
        "content-type is text/event-stream",
        headers.get("content-type", "").startswith("text/event-stream"),
        headers.get("content-type", ""),
    )

    events = sse_events(first)
    kinds = [kind for _, kind, _ in events]
    record("stream carries status events", "status" in kinds)
    record("stream ends with done", kinds[-1] == "done", str(kinds[-3:]))

    streamed = [data for _, kind, data in events if kind == "finding"]
    record("one finding event per finding", len(streamed) == len(result["findings"]))
    record("stream findings match the result exactly", streamed == result["findings"])
    keys = [(f["path"], f["line"], f["ruleId"]) for f in streamed]
    record("stream is ordered", keys == sorted(keys))

    done = events[-1][2]
    record(
        "done carries total and usage",
        done.get("total") == len(result["findings"]) and done.get("usage") == result["usage"],
        json.dumps(done),
    )

    second = await p.stream(job_id)
    record("replay of a finished job is byte-identical", first == second)


async def check_rate_limit(p: Probe) -> None:
    print("\nrate limiting")
    declared = p.limits["rateLimitPerMinute"]
    body = {"diff": diffs.NEW_FILE}

    responses = await asyncio.gather(
        *(p.post(body) for _ in range(declared * 2)), return_exceptions=True
    )
    statuses = [r.status_code for r in responses if isinstance(r, httpx.Response)]
    limited = [r for r in responses if isinstance(r, httpx.Response) and r.status_code == 429]

    record("burst is shed with 429", len(limited) > 0, f"statuses={sorted(set(statuses))}")
    record("no 5xx under burst", not any(s >= 500 for s in statuses), str(sorted(set(statuses))))
    if limited:
        record(
            "429 carries Retry-After and the envelope",
            "retry-after" in limited[0].headers
            and limited[0].json().get("error", {}).get("code") == "rate_limited",
            str(dict(limited[0].headers)),
        )

    # GETs are never limited, even right after a burst.
    job = await p.client.get(f"{p.base}/v1/reviews/none", headers=p.auth)
    record("GET is not rate limited", job.status_code == 404, str(job.status_code))

    # Give the bucket time to refill before the remaining checks.
    await asyncio.sleep(3)


async def check_concurrency(p: Probe) -> None:
    print("\nconcurrency")
    payloads = [
        diffs.large_multifile_diff(file_count=8, lines_per_file=40 + n) for n in range(5)
    ]
    started = time.monotonic()
    ids = []
    for payload in payloads:
        ids.append(await p.submit(payload))
    results = await asyncio.gather(*(p.wait(job_id) for job_id in ids))
    elapsed = time.monotonic() - started

    record("five concurrent jobs all reach done", all(r["status"] == "done" for r in results))
    record("the queued fifth job did not fail", all(r["status"] != "failed" for r in results))
    record("all five finished inside the budget", elapsed < 30.0, f"{elapsed:.1f}s")


async def check_llm(p: Probe) -> None:
    print("\nllm provider")
    try:
        result = await p.wait(await p.submit(diffs.ALL_RULES, {"provider": "llm"}), timeout=60)
    except TimeoutError as exc:
        record("llm job reaches a terminal state", False, str(exc))
        return

    if result["status"] == "done":
        record("llm path completes", True, f"{len(result['findings'])} findings")
        record(
            "llm findings use the documented shape",
            all(
                set(f) == {"id", "ruleId", "path", "line", "severity", "category", "title", "evidence"}
                for f in result["findings"]
            ),
        )
    else:
        record(
            "llm path degrades gracefully with a clear error",
            result["status"] == "failed" and bool(result.get("error", {}).get("message")),
            json.dumps(result)[:200],
        )

    health = await p.client.get(f"{p.base}/health")
    record("service still healthy after the llm path", health.status_code == 200)


# --------------------------------------------------------------------------- #
async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        print("usage: python scripts/probe.py <base_url> <bearer_token>")
        return 2

    probe = Probe(sys.argv[1], sys.argv[2])
    print(f"probing {probe.base}")
    try:
        await check_public(probe)
        await check_auth(probe)
        await check_errors(probe)
        await check_mock_findings(probe)
        await check_injection(probe)
        await check_chunking(probe)
        await check_cache_and_idempotency(probe)
        await check_sse(probe)
        await check_concurrency(probe)
        await check_llm(probe)
        await check_rate_limit(probe)      # last: it deliberately exhausts the budget
    finally:
        await probe.close()

    failures = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed")
    for _, name, detail in failures:
        print(f"  FAIL {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
