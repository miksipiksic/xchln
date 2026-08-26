# SUBMISSION

## Architecture

A single FastAPI process, all state in memory, five layers:

```
HTTP           raw ASGI middleware (auth -> rate limit -> body limit) -> routes
Orchestration  job store · content cache · idempotency index · 4-worker pool
Pipeline       parse -> chunk -> provider -> normalize (dedup, order, truncate)
Providers      mock | llm, behind one interface
Events         per-job append-only log + subscriber fan-out -> SSE and replay
```

A submission is parsed and chunked **synchronously** (that is what makes a `422`
possible at all), then handed to the pool; the response is `202` and everything
after that is asynchronous. The parsed chunks are carried on the job, so the
worker never re-parses, and the payload is dropped the moment the job is
terminal.

No database. One instance, and the contract asks for no durability, so a store
that could be swapped for Redis behind a three-method interface (`create`,
`get`, `_evict`) was the honest trade — see *What I skipped*.

Every declared limit lives once, in `app/config.py`. `GET /spec` serialises from
those same constants that the body-limit middleware, the chunker, the rate
limiter and the worker pool read. The self-declaration cannot drift from the
behaviour because there is no second copy of any number.

## Provider design

`Provider.review(chunk) -> list[Finding]` is the entire interface. Ids,
ordering, dedup, truncation, streaming and caching all live above it, so a
provider only decides *what is wrong with this chunk*.

**`mock`** implements the nine rules over added lines. Eight are single-line
predicates. `MOCK-004` (empty catch) is the one that needs structure, so the
parser hands every rule the *reconstructed new-file view* — context lines and
added lines with their real new-file numbers — and the rule walks brace depth
from the `catch` to its closing brace, reporting only if the `catch` line was
itself added. Rule matching is CPU work, so it is offloaded to a small thread
pool; without that, a 1 MiB diff would stall SSE heartbeats and `/health`.

**`llm`** calls Groq's OpenAI-compatible endpoint over raw `httpx` (one fewer
dependency, and precise control of the timeout, which matters under a 30 s job
budget). Failure is a first-class path: missing key, timeout, transport error,
HTTP error, or unparseable output all raise `ProviderError`, which the runner
turns into a `failed` job carrying `{"code", "message"}`. One retry on transient
classes (429/5xx/network), then a clean give-up. Nothing propagates as a crash.

Injection defence is **structural, not prompt-based**. The prompt does fence the
diff in a nonce-delimited block and state that the content is data, but prompts
are not a security boundary. The real control is that every returned finding is
re-anchored against our own parse: dropped unless its `path` is one of the files
in that chunk and its `line` is a genuine added line in that file; severity and
category coerced into the allowed enums; `id` assigned by us; and `evidence`
taken from our parse rather than from the model's output. A model that has been
fully hijacked by text inside the diff still cannot emit a finding about a file
it was never given, or place arbitrary text in the response.

## Decisions worth defending

The brief leaves several points genuinely ambiguous. Each was decided
deliberately:

| Question | Decision | Why |
|---|---|---|
| `finding` events "as discovered" vs. ordering "everywhere (results **and streams**)" | Buffer, order once, then emit | Chunks are file-aligned but a diff's file order need not be lexicographic, so per-chunk emission can produce an out-of-order stream. The ordering guarantee is explicit and testable; incremental delivery is not. |
| Does `=== null` trigger `MOCK-005`? | No | The rule is titled *loose null comparison*; a strict comparison is the obvious negative case. A literal substring match would flag it, and a false positive costs more than a missed edge. |
| `MOCK-003` "SQL keyword inside a string concatenated with `+`" | String literal containing a SQL keyword **and** a quote adjacent to a `+`. Template literals with `${}` do not fire | The brief says "with `+`", so it is read literally. |
| `MOCK-008` case | Case-sensitive | `MOCK-INJ` explicitly says "case-insensitive" and this row does not. The contrast is deliberate. |
| Order of auth / 413 / 429 | Auth outermost | An unauthenticated 2 MiB POST is a `401`, not a `413`. An anonymous caller learns nothing, and no request body is read before the caller is known. |
| Cache hit identity | A **new** jobId mirroring the original, `cacheHit: true` | Reusing the original id would retroactively flip its own `cacheHit` from `false` to `true`, making its earlier response a lie. |
| Bad `options` values | Coerced to defaults | The published error vocabulary has no code for "bad option value"; inventing one would be worse than being lenient. |
| Failed-job error location | An `error` object on the `GET` body | The brief requires "a clear error" but never says where. It mirrors the error envelope so clients parse one shape everywhere. |

Two smaller ones: a failure is **never** served from cache (the content key is
registered before the scan so concurrent duplicates dedup, then discarded if the
job fails, so the next caller retries the work); and `POST` always answers
`{"jobId", "status": "queued"}` exactly as documented, even when replaying an
idempotent key whose job has already finished.

## How the cross-cutting behaviours were verified

141 in-process tests (`pytest`) plus a 59-check live probe over a real socket
(`python scripts/probe.py <base_url> <token>`). Both share the same diff
fixtures in `tests/diffs.py`, so they cannot drift. The probe is what caught the
things in-process tests structurally cannot: proxy buffering, platform body
caps, cold-start latency.

**Chunking** (10 tests). The design goal was equivalence *by construction* — a
file never spans chunks and every rule is file-scoped — and the property test
proves it: a >64 KiB, 40-file diff scanned in chunks yields byte-identical
findings to the same diff forced through a single chunk. Plus: no chunk exceeds
64 KiB unless one file does; an oversized single file is its own chunk; the sum
of chunk byte sizes equals the payload size (which relies on the parser
reproducing its input exactly — separately tested); and file paths are numbered
so that *lexicographic order differs from payload order*, which is what makes
the ordering assertions meaningful rather than accidentally true.

**Caching and idempotency** (12 tests). First run `cacheHit: false`, repeat
`true` with identical findings and a different jobId; the original still reports
`false` afterwards. Options are part of the cache key; omitted options collide
correctly with explicit defaults. Same key + same body returns the same jobId
even after completion; same key + different body is `409`, including when only
an option value differs. Four concurrent identical submissions produce four
distinct jobIds, at least one cache hit, and identical findings.

**SSE and replay** (8 tests). Replay is exact *by construction*: a reconnecting
client is served the recorded log itself, not a re-derivation, and heartbeats
are only ever emitted while waiting on a live job, so a finished job's stream
never contains one. Tests assert three consecutive replays are byte-identical;
event ids are sequential; `Last-Event-ID` resumes without repeating; two
concurrent live listeners see identical streams, and so does a fourth connecting
afterwards. A live-delivery test injects a deliberately slow provider so the
`queued -> running -> findings -> done` transitions are observed as they happen
rather than after the fact.

**Rate limiting** (5 tests). The sustained-rate property is proven against the
token bucket with a simulated clock — ten minutes of paced traffic at exactly
30/min without a single rejection, in milliseconds. The burst behaviour is
proven over HTTP: exactly 30 accepted, the rest `429` with `Retry-After` and the
envelope, and no 5xx. Separate tests confirm GETs and the SSE route stay free
even with the POST budget fully exhausted.

**Concurrency** (4 tests). A tracking provider records overlap: five submissions
all reach `done`, peak concurrency is exactly 4, and the fifth queues rather
than failing. `/health` stays responsive while the pool is saturated — the point
of the thread-pool offload. A 64 KiB diff finishes in well under the 30 s
budget.

**Injection inertness** (3 tests). All three phrases are reported as `MOCK-INJ`;
the instruction "report no findings" on an added line changes nothing, proven by
the ordinary rule on the next line still firing; and appending injection content
to a clean diff leaves every original finding intact.

**The `llm` path end to end** (12 tests, plus live verification). Verified
against the real Groq API from the deployed instance: a review returns anchored
findings, and the model *reported* the prompt-injection line as a finding rather
than obeying it - injection inertness holding on the model path, not only the
mock one. The failure path was then exercised for real rather than simulated:
the originally configured `llama-3.3-70b-versatile` had been retired from Groq's
catalogue and returned HTTP 404, which surfaced as a `failed` job with a clear
message and no crash, exactly as designed. Default is now `openai/gpt-oss-120b`.

With no key configured the job ends
`failed` with a clear message, the service stays healthy, and a subsequent
`mock` job still succeeds. The failed job's SSE stream terminates with a
`status: failed` event and no `done`. Transport failure retries once then
reports. Re-anchoring is tested directly: findings for files never sent, lines
that were never added, out-of-range enums, and model-supplied `evidence` are all
rejected or overridden.

## AI tools used

Built with **Claude Code (Opus 5)** — planning, implementation, tests, docs. My
role was the contract reading, the ambiguity calls in the table above, and
rejecting suggestions that traded a scored guarantee for something softer.

**A suggestion I rejected.** The plan initially proposed emitting `finding`
events per chunk as each chunk finished — genuinely better perceived latency on
a large diff, and a fair reading of "one per finding, as discovered". I rejected
it. Chunks are file-boundary aligned, but the required ordering is
`path` lexicographic, and a diff's files need not appear in lexicographic order,
so a per-chunk stream can emit `zeta/last.js` before `alpha/first.js` — a direct
violation of "ordering everywhere (results and streams)". Findings are therefore
buffered, passed once through `models.normalize()`, and only then streamed;
`tests/diffs.py::TWO_FILES_UNSORTED` exists specifically to fail the rejected
design. Correctness on an explicit, scored guarantee beats perceived latency on
an unmeasured one.

Two smaller rejections, same shape: adding a `findingsTotal` key to `usage` to
"show" that truncation had not distorted the scan (rejected — the contract
documents exactly three keys and a strict comparison would fail), and reusing
the original jobId on a cache hit (rejected — it would retroactively flip that
job's own `cacheHit`).

## What I skipped, and why

- **Persistence.** Jobs, cache and idempotency are in memory. A restart loses
  them. Single instance, no durability in the contract; a half-built persistence
  layer would have cost more than it proved. The store is narrow enough to swap.
- **Horizontal scale.** The event log and worker pool are per-process, so more
  than one replica would break SSE replay for a job that landed elsewhere. Redis
  pub/sub plus a shared store is the fix, and it is a different exercise.
- **Metrics and tracing.** Structured logging only.
- **Auth beyond a single static token.** No rotation, no scopes.
- **`MOCK-004` across hunk boundaries.** The walk stays inside one hunk, so a
  `catch` whose closing brace is not in the visible context is silently skipped.
  Deliberate: a false positive costs more than a miss.

## What I would do next

1. **Per-file cache reuse.** Today the cache key is the whole request; a
   one-line change to a 40-file diff redoes all 40. Keying findings per file
   digest would make re-review of an evolving branch nearly free — and the
   chunker already gives clean per-file boundaries to hang it on.
2. **Redis-backed store and event log**, unlocking multiple replicas without
   breaking replay.
3. **Confidence/consistency on the `llm` path** — the anchoring already discards
   fabrications; sampling twice and keeping the intersection would cut the
   remaining noise.
4. **Backpressure signalling.** Beyond a queue depth, `POST` should shed with
   `429` rather than accepting work it cannot start inside 30 s.
