# OCR architecture

Entry points: `app.service.create_pipeline`, `app.__main__.run_ocr`,
`app.api.lifespan`, `app.api.ocr`, `app.api.stream_ocr`.

## Processing

```text
CLI file / API upload
  -> Pipeline.submit: job-owned disk copy
  -> inspect_input: content detection + page count / raster validation
  -> page-N:image       prepare_page, PDF render or normalized raster
  -> page-N:llm         OllamaOCRClient.recognize, one request/page
  -> page-N:postprocess build_document + render_markdown + PNG crops
  -> assemble_document: ordered document.md + adjacent crops
  -> archive_document: disk-backed ZIP
```

- Shared asyncio coordinator; ready pages advance independently.
- Defaults: 300 DPI; 4 image processes, 1 LLM thread, 4 postprocess processes.
  PDFium runs in spawned processes; LLM clients belong to executor threads.
- `PipelineSettings` in `app.config`; optional `pipeline` config mapping.
  CLI overrides all pipeline settings; multipart `pdf_dpi` overrides resolution.
- Per-stage ready+running <= 2*workers; round-robin admission across jobs.
  Global in-flight page window: 2*sum(worker counts).
- PDF output: `<!-- Page N -->`, source page order, unique
  `page-0001-image-001.png` crops. Raster formatting/names stay compatible.
- Expected page errors -> placeholders, skip dependent tasks, continue remaining
  pages. Partial success publishes output; all-page or infrastructure failure does not.
- Single-image helpers remain: `OCRService.ocr -> OCRDocument`,
  `prepare_image`, `convert_image`, `export_document`.

Detailed task states, scheduling and ownership:
[PDF pipeline](pdf-pipeline.md). Grounding/parser/crop contracts:
[LLM output processing](llm-output-processing.md).
Wire responses and client parsing: [API event examples](api-events.md).

## Interfaces and lifetime

| Entry | Result |
| --- | --- |
| CLI `ocr FILE --output-path PARENT` | unique directory; stdout path, stderr progress |
| `POST /api/ocr` | blocking ZIP; `X-OCR-Status`, `X-OCR-Failed-Pages` |
| `POST /api/ocr/stream` | continuous SSE; execution survives disconnect |
| `GET /api/ocr/jobs/{id}` | task snapshot + statistics |
| `GET /api/ocr/jobs/{id}/events` | SSE replay/live events; `Last-Event-ID` |
| `GET /api/ocr/jobs/{id}/download` | repeatable ZIP until expiry |
| `GET /api/health` | application liveness, no Ollama call |

- CLI exits: 0 complete, 2 partial, 1 failed/interrupted.
- Blocking errors: input 400, backend/output 502, timeout 504, infrastructure 500.
  Form validation 422; job lookup 404; unavailable download 409.
- Streaming errors after acceptance use terminal SSE events.
- Completed jobs/artifacts expire after 3600 seconds. Active downloads pin files;
  expiry rejects new lookups/downloads without interrupting existing transmissions.
- `JobZipResponse` releases pins in `finally`; shutdown drains workers/downloads.
  Blocking `TemporaryZipResponse` owns its detached workspace through transmission.
- One API process; in-memory job state is lost on restart. No durable queue.
  Web UI unchanged; PDF controls/progress are CLI/API features.

## Input and model contracts

- Detect input content independently of filename/MIME type. Read streams from their
  current position; spool before request-owned uploads close.
- PNG/JPEG/WebP bytes pass through when no normalization is needed.
  BMP/GIF/TIFF/HEIF/AVIF use installed Pillow codecs; EXIF transpose, white alpha
  background, RGB PNG. Multi-frame raster images remain unsupported.
- PDF pages render individually at `scale=pdf_dpi/72`, RGB on white, respecting
  rotation. Invalid/empty/locked PDFs fail inspection.
- `load_settings`: repository-root config independent of cwd; required Ollama
  URL/model/timeout; pipeline defaults are positive integers.
- `recognize`: grounding Markdown prompt, one image, `stream=False`; raw response
  text preserved. No startup inference/model download or automatic retry.
- `build_document`: ordered grounded blocks; normalized 0–999 coordinates,
  per-axis pixel scaling, exact `image` labels become PNG crops.
  Malformed grounding or empty crops -> `OCROutputError`.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -q
```

- 2026-10-27: **60 tests passed**, no live Ollama requests in the suite.
- `tests/test_pipeline.py`: real PDF rendering/spawned workers, controlled overlap,
  shared limits/fairness/backpressure, partial/all-failed jobs, replay/heartbeat,
  disconnect survival, expiry/download pinning and shutdown.
- Existing processing, grounding, SDK mock transport, CLI/API equivalence and
  export/transmission cleanup regressions remain covered.
- Live two-page default-settings smoke test succeeded; metrics in
  [pdf-pipeline.md](pdf-pipeline.md#verification).
- No dependencies added. `app/requirements.txt` remains authoritative.
