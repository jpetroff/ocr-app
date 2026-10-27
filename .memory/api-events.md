# OCR API event examples

Source contracts: `app.api.stream_ocr`, `job_events`, `job_status`,
`app.pipeline.Job.emit`, `Job.snapshot`, `Pipeline._sse`.

## Copyable responses

Examples use application serializers with deterministic IDs/times and no LLM
requests. URLs identify illustrative jobs, not live server resources. Streams
contain every event for their scenario; concurrent execution can interleave pages
differently. Sample task durations are zero to keep fixtures deterministic.

| Scenario | Complete SSE response body | Polling JSON response |
| --- | --- | --- |
| Two pages succeed | [ocr-completed.sse](examples/ocr-completed.sse) | [ocr-completed.json](examples/ocr-completed.json) |
| Page 2 times out; page 1 retained | [ocr-partial.sse](examples/ocr-partial.sse) | [ocr-partial.json](examples/ocr-partial.json) |
| Invalid/locked PDF fails inspection | [ocr-failed.sse](examples/ocr-failed.sse) | [ocr-failed.json](examples/ocr-failed.json) |
| Reconnect cursor falls outside history | [ocr-snapshot.sse](examples/ocr-snapshot.sse) | Same snapshot shape as polling JSON |

## Request and framing

```sh
curl -N -H 'Accept: text/event-stream' \
  -F 'file=@book.pdf' -F 'pdf_dpi=300' \
  http://127.0.0.1:8000/api/ocr/stream
```

```http
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
cache-control: no-cache
x-accel-buffering: no

id: 1
event: job.accepted
data: {"sequence":1,"job_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","timestamp":"2026-10-27T08:00:00+00:00","stats":{"stages":{"image":{"pending":1}},"pages_total":0,"pages_completed":0,"pages_failed":0}}

```

- A blank line terminates each event. Network chunks can contain partial or
  multiple events; decode UTF-8 incrementally and buffer until the blank line.
- Event type is the SSE `event:` field, not a property in the JSON data.
- Normal events share `sequence`, `job_id`, `timestamp`, `stats`.
  `sequence` matches SSE `id` and increases within one job; timestamps use UTC.
- `stats.stages` maps stage -> status -> count. Missing counts mean zero.
  Stage names: `image`, `llm`, `postprocess`. Image counts include inspection;
  postprocess counts include document assembly and ZIP creation.
- `pages_total` is zero before inspection. Progress concerns complete page tasks,
  not LLM tokens or recognized text. No Markdown/image bytes stream in these events.
- A heartbeat every 15 idle seconds is a comment, without ID or JSON:

```text
: heartbeat

```

## Event payloads

All normal event payloads include the common fields above. Extra fields:

| SSE event | Extra fields / client action |
| --- | --- |
| `job.accepted` | Save job ID immediately for reconnection. |
| `job.planned` | `pages`, `is_pdf`; initialize page rows. |
| `task.updated` | `task`; update by task ID, not arrival position. |
| `page.completed` | `page`; inspect `error` to distinguish success/failure. |
| `job.completed` | `result`, `error: null`; expose ZIP download. |
| `job.partial` | `result`, `error: null`; expose ZIP and failed-page summary. |
| `job.failed` | `result`, `error`; stop progress, no download. |
| `job.snapshot` | Different shape: polling snapshot, described below. |

Example nested task from `task.updated`:

```json
{
  "id": "page-1:llm",
  "stage": "llm",
  "page": 1,
  "dependencies": ["page-1:image"],
  "status": "running",
  "started_at": "2026-10-27T08:00:15+00:00",
  "finished_at": null,
  "duration_seconds": null,
  "error": null
}
```

Task states: `pending`, `ready`, `running`, `completed`, `failed`, `skipped`,
`cancelled`. IDs are `inspect`, `page-N:image`, `page-N:llm`,
`page-N:postprocess`, `assemble`, `archive`. Non-page tasks have `page: null`.

Example nested page from `page.completed` on failure:

```json
{
  "page": 2,
  "markdown": null,
  "images": [],
  "error": "Ollama OCR request timed out.",
  "failed_stage": "llm"
}
```

Successful pages have `error: null`, `failed_stage: null`, and relative image
filenames in `images`. `markdown` is null in public page events; download the
finished document to obtain its content. `page.completed` also signals page
failure; its name means processing ended, not recognition succeeded.

Example `result` from terminal `job.partial`:

```json
{
  "status": "partial",
  "failed_pages": [
    {
      "page": 2,
      "markdown": null,
      "images": [],
      "error": "Ollama OCR request timed out.",
      "failed_stage": "llm"
    }
  ],
  "download_url": "/api/ocr/jobs/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/download",
  "expires_at": "2026-10-27T09:00:46+00:00"
}
```

Completed results have no failed pages. Failed results have `download_url: null`;
inspection failures can have no failed-page entries because page planning never
finished. Only terminal events or terminal snapshot `status` establish completion;
an intermediate snapshot can contain result metadata while archiving still runs.

## Reconnection and polling

Reconnect using GET, preserving the last processed sequence. Do not repeat the
upload POST to reconnect: it creates another job and additional LLM requests.

```sh
curl -N -H 'Last-Event-ID: 20' \
  http://127.0.0.1:8000/api/ocr/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/events
curl http://127.0.0.1:8000/api/ocr/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The GET stream replays events after the cursor, then follows live events. The
server retains 512 events. A stale or future cursor gets `job.snapshot` with:

```text
id: <current sequence>
event: job.snapshot
data: {job_id, sequence, status, stats, tasks, result, error}
```

The diagram above shows field names, not literal JSON. The complete valid payload
is in [ocr-snapshot.sse](examples/ocr-snapshot.sse). Snapshots have no `timestamp`;
their `status` is `running`, `completed`, `partial`, or `failed`. Replace local
task state from `tasks`, adopt its cursor, then apply subsequent events. Polling
returns this same JSON shape. Ignore replayed sequences already applied.

A terminal snapshot can be the only reconnection response before EOF. EOF alone
does not establish success; reconnect/poll if no terminal status was observed.
Completed jobs expire after one hour; jobs are not retained across server restart.

## Browser fetch example

Use [consume-ocr-events.js](examples/consume-ocr-events.js) as a copyable parser.
It handles UTF-8/chunk boundaries, CRLF/LF framing and heartbeat comments. These
requests assume the existing same-origin `/api` proxy. Native `EventSource`
cannot submit the multipart upload or set a custom reconnection header, so use
`fetch` for both requests.

```js
import { consumeOcrEvents } from "./consume-ocr-events.js";

const cursor = { jobId: null, sequence: 0, terminal: false };

function applyEvent({ event, id, data }) {
  if (event !== "job.snapshot" && data.sequence <= cursor.sequence) return;
  cursor.jobId = data.job_id;
  cursor.sequence = id ?? data.sequence;
  console.log(event, data); // Replace with page/task UI updates.
  const status = event === "job.snapshot" ? data.status : event.slice(4);
  cursor.terminal = ["completed", "partial", "failed"].includes(status);
  if (cursor.terminal && data.result?.download_url) {
    console.log("Download:", data.result.download_url);
  }
}

export async function startOcr(file, signal) {
  // One cursor per job; reset before starting a different upload.
  Object.assign(cursor, { jobId: null, sequence: 0, terminal: false });
  const body = new FormData();
  body.append("file", file);
  body.append("pdf_dpi", "300");
  await consumeOcrEvents(await fetch("/api/ocr/stream", {
    method: "POST", body, signal,
    headers: { Accept: "text/event-stream" },
  }), applyEvent);
}

export async function resumeOcr(signal) {
  if (!cursor.jobId || cursor.terminal) return;
  await consumeOcrEvents(await fetch(`/api/ocr/jobs/${cursor.jobId}/events`, {
    signal,
    headers: {
      Accept: "text/event-stream",
      "Last-Event-ID": String(cursor.sequence),
    },
  }), applyEvent);
}
```

On a dropped connection, call `resumeOcr`; retain the cursor while the job runs.
Aborting fetch closes the subscriber and leaves the server job running. External
clients use the same request headers, frame parsing and sequence rules. After
completion, GET `result.download_url` to receive `application/zip`.

## HTTP errors before a stream opens

| Request / condition | Status | Example JSON |
| --- | --- | --- |
| Invalid `Last-Event-ID` | 400 | `{"detail":"Last-Event-ID must be a nonnegative integer."}` |
| Unknown/expired job | 404 | `{"detail":"Job not found or expired."}` |
| Download still running | 409 | `{"detail":"Document is still processing."}` |
| Download after failed job | 409 | `{"detail":"Job has no downloadable document."}` |
| Missing file / invalid DPI | 422 | FastAPI validation `detail` array |

After upload acceptance, invalid PDF/backend failures arrive in `job.failed`
inside HTTP 200 SSE. Always check both the HTTP status and terminal job status.

## Verification

- Serializer fixtures: completed 32 frames, partial 30 frames, failed 5 frames;
  contiguous IDs, valid JSON, terminal results match polling responses.
- `consume-ocr-events.js`: all four SSE fixtures parsed with one-byte chunks,
  CRLF separators and heartbeat comments; split multibyte UTF-8 also verified.
- Documentation/examples only; no API behavior or dependencies changed.
