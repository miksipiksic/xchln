# AI Diff Review Service

An HTTP service that accepts a unified diff, reviews it asynchronously through a
pluggable provider, and returns structured findings over polling or Server-Sent
Events.

Two providers sit behind one interface:

- **`mock`** — fully deterministic rule engine (the nine `MOCK-*` rules). Default.
- **`llm`** — a real model (Groq's OpenAI-compatible endpoint) behind the same
  pipeline. Credentials live only on the server.

See [SUBMISSION.md](SUBMISSION.md) for the architecture, the design decisions and
how each cross-cutting behaviour was verified.

## Running it

```bash
# dependencies
pip install -e ".[dev]"          # or: uv sync --extra dev

# a bearer token is required - the service fails closed without one
export API_BEARER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

With Docker:

```bash
docker build -t ai-diff-review .
docker run -p 8000:8000 -e API_BEARER_TOKEN=your-token ai-diff-review
```

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_BEARER_TOKEN` | **yes** | — | Token for all `/v1/*` routes. Unset ⇒ every `/v1` request is `401`. |
| `PORT` | no | `8000` | Listen port (free-tier hosts usually inject this). |
| `GROQ_API_KEY` | for `llm` | — | Groq API key. Without it, `llm` jobs fail cleanly; `mock` is unaffected. |
| `LLM_BASE_URL` | no | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint. |
| `LLM_MODEL` | no | `openai/gpt-oss-120b` | Model id. |
| `LLM_TIMEOUT_SECONDS` | no | `20` | Per-request ceiling, kept under the 30 s job budget. |
| `LLM_MAX_CHUNKS` | no | `8` | Cap on chunks sent to the model per job. |

The declared limits in `GET /spec` are not configurable by design — they are
constants in `app/config.py`, read by the middleware, the chunker, the worker
pool *and* the spec route, so the self-declaration cannot drift from behaviour.

## API

| Route | Auth | Notes |
|---|---|---|
| `GET /health` | public | `{status, version, uptimeSeconds}` |
| `GET /spec` | public | declared limits and providers |
| `POST /v1/reviews` | bearer | `202 {jobId, status:"queued"}`; honours `Idempotency-Key` |
| `GET /v1/reviews/{jobId}` | bearer | status, findings when `done`, usage |
| `GET /v1/reviews/{jobId}/stream` | bearer | SSE: `status`, `finding`, `done`; replays in full |

```bash
TOKEN=your-token
DIFF=$(git diff HEAD~1 | python -c 'import json,sys; print(json.dumps(sys.stdin.read()))')

JOB=$(curl -sS -X POST localhost:8000/v1/reviews \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"diff\": $DIFF}" | python -c 'import json,sys; print(json.load(sys.stdin)["jobId"])')

curl -sS localhost:8000/v1/reviews/$JOB -H "Authorization: Bearer $TOKEN"
curl -sSN localhost:8000/v1/reviews/$JOB/stream -H "Authorization: Bearer $TOKEN"
```

Request body:

```json
{
  "diff": "<unified diff>",
  "options": { "provider": "mock", "maxFindings": 100 }
}
```

Errors always use the envelope `{"error": {"code", "message"}}` with one of:
`unauthorized`, `payload_too_large`, `invalid_json`, `invalid_diff`,
`idempotency_conflict`, `not_found`, `rate_limited`, `internal`.

## Tests

```bash
pytest                                        # 141 in-process tests
python scripts/probe.py http://localhost:8000 "$API_BEARER_TOKEN"   # live contract probe
```

`scripts/probe.py` runs the contract over a real socket against a deployed
instance and exits non-zero on any failure. It shares its diff fixtures with the
pytest suite so the two cannot drift.

## Layout

```
app/
  config.py       every declared limit, in one place
  main.py         app factory, middleware order, error envelope
  models.py       Finding/Job/Usage + the single ordering chokepoint
  deps/           auth, rate limit, body limit (raw ASGI middleware)
  diff/           unified diff parser and file-boundary chunker
  providers/      base interface, mock rule engine, llm client
  jobs/           store, content cache, idempotency index, worker pool
  events/         per-job event log and SSE rendering
```
