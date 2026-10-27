# PDF pipeline

Entry points: `app.service.create_pipeline`, `app.pipeline.Pipeline.submit`,
`app.__main__.run_ocr`, `app.api.stream_ocr`.

Implemented 2026-10-27. No dependencies added; CLI/API changes only.

```yaml
pipeline:
  pdf_dpi: 300
  image_workers: 4
  llm_workers: 1
  postprocess_workers: 4
```

```sh
.venv/bin/python -m app ocr book.pdf --pdf-dpi 300 --output-path output
curl -N -F 'file=@book.pdf' -F 'pdf_dpi=300' http://127.0.0.1:8000/api/ocr/stream
```

```text
request stream -> job /tmp/ocr-*/input (copy before dispatch)
inspect_input -> JobPlan
page-N:image -> prepared-N.png
page-N:llm -> raw-N.txt
page-N:postprocess -> page-N/page.md + crop PNGs
assemble -> <safe-stem>-<uuid>/document.md + adjacent crops
archive -> document.zip
```

## Workers and state

- `PipelineSettings`: DPI 300; image 4, LLM 1, postprocess 4. Positive integers.
  Config: optional `pipeline`; CLI flags override. API overrides only DPI.
- `Pipeline`: one asyncio owner of `JobPlan`, `TaskRecord`, `PageResult`,
  `JobResult`. API lifespan owns one instance; CLI owns one per invocation.
- Image/postprocess: `ProcessPoolExecutor`, explicit `spawn`. PDFium inspection,
  page access, rendering and cleanup occur entirely inside image workers.
- LLM: bounded `ThreadPoolExecutor`; thread-local `OllamaOCRClient`; clients close
  after executor shutdown. One request per prepared page, no retries.
- `_admit`, `_fair_task`: round-robin jobs; per-stage ready+running <= 2*workers,
  running <= workers. Pending tasks are metadata only. A global page window of
  2*sum(worker counts) bounds upstream artifacts awaiting downstream admission.
- Task IDs: `inspect`, `page-1:image`, `page-1:llm`, `page-1:postprocess`,
  `assemble`, `archive`. Dependencies, status, timestamps, elapsed seconds, error.
- States: pending -> ready -> running -> completed/failed;
  downstream skipped on page failure, queued work cancelled on shutdown.
- `build_document` parses/crops the exact inference image; `render_markdown`
  preserves content. Workers return paths/metadata, never decoded rasters.
- PDF crops: `page-0001-image-001.png`. Raster names/Markdown remain compatible.
- `assemble_document`: numeric page order, incremental text copies;
  `<!-- Page N -->` before every PDF page, including blank/failed pages.

```text
JobPlan(tasks: dict[str, TaskRecord], page_count, is_pdf)
TaskRecord(id, stage, page, dependencies, status,
           started_at, finished_at, duration_seconds, error)
PageResult(page, markdown, images, error, failed_stage)
JobResult(status, directory, archive, failed_pages)
```

`PageResult.markdown` stores an internal path. API page events omit that path;
public result metadata exposes a download URL instead of artifact paths.

## Results and lifetime

| Case | Result |
| --- | --- |
| All pages succeed | completed |
| Some pages fail | partial, placeholders + failed_pages |
| No page succeeds | failed, no download |
| Input inspection/infrastructure/final export fails | failed, no download |

- Expected page errors skip dependent stages; other pages continue. Disk failures
  and broken pools fail the job. Single-page errors preserve their exception type
  for blocking HTTP 400/502/504 mapping.
- `Pipeline.close`: stop admission, cancel queued work, drain active workers/downloads,
  join executors, close clients, remove workspaces. No file removal under workers.
- Streaming job execution belongs to coordinator, independent of subscribers.
  State is process-local; no restart persistence or multi-process coordination.
- Terminal state/artifacts retained 3600 seconds. `Pipeline.expire` sweeps every
  second and on lookup. `JobZipResponse` pins files from construction through
  transmission/error/cancellation; expiry waits for active downloads.
  `Pipeline.get` rejects expired jobs even while an existing download pins files.
  Failed/cancelled jobs do not consume the page window for later submissions.
- Blocking API detaches completed workspace into `TemporaryZipResponse`, retaining
  its existing cleanup-on-transmission contract. Partial headers identify failures.
- CLI copies completed artifacts into a unique output child; stdout path only,
  stderr progress/errors. Exit 0 complete, 2 partial, 1 failed/interrupted.
- Legacy `OCRService.ocr` remains an in-process single-image helper; CLI/API use
  the concurrent pipeline.

## Streaming interfaces

Complete SSE/polling fixtures, payload fields and browser/external-client usage:
[API event examples](api-events.md).

```text
POST /api/ocr/stream                 file, optional pdf_dpi -> SSE
GET  /api/ocr/jobs/{id}               snapshot
GET  /api/ocr/jobs/{id}/events        SSE, optional Last-Event-ID
GET  /api/ocr/jobs/{id}/download      ZIP, repeatable until expiry
POST /api/ocr                        same pipeline, blocking ZIP
```

- `Job.emit`: per-job monotonic sequence, timestamp, aggregate counts, details.
- Events: job.accepted, job.planned, task.updated, page.completed,
  job.completed/partial/failed. Terminal results: status, failed_pages,
  download_url, expires_at. Task snapshots include dependencies, status and timing;
  aggregate statistics include per-stage status counts and completed/failed pages.
- `Pipeline.events`: bounded 512-event history; stale/future cursor ->
  job.snapshot at current cursor, then live events. Subscriber closure cannot
  cancel execution. Heartbeat comments every 15 seconds. No-cache/no-buffer headers.
- Before acceptance: ordinary HTTP validation errors. After acceptance: terminal
  SSE error. Downloads: 409 while running/failed, 404 after expiry.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -q
# 2026-10-27: Ran 60 tests; OK (no live Ollama calls).
```

- `tests/test_pipeline.py`: generated deterministic PDFs (blank/rotated pages),
  dimensions/DPI, real spawned workers, unique crops, controlled stage overlap,
  shared limits/fairness/backpressure, thread-owned clients, partial/all-failed
  results, export failures, cancellation, event replay/heartbeat, download pinning.
  Direct ASGI checks verify delivery before completion and disconnect survival;
  shutdown tests hold downloads open and verify cleanup after failed transmission.
- Existing raster, grounding, SDK transport, CLI/ZIP equivalence and response
  cancellation tests remain in the unittest suite.
- Live smoke: configured `deepseek-ocr:3b`, two copies of the simple PDF sample,
  default DPI/workers -> exit 0, 3030 Markdown characters, two ordered page
  markers, two PNG crops, no failure placeholders (2026-10-27).
