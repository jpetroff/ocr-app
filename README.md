# ocr-app

Image and PDF OCR with a Python CLI, FastAPI backend, and TypeScript/Vite
frontend. Ollama recognizes each prepared page in a separate request. Output is
one Markdown document with extracted image crops, delivered as a directory or ZIP.

## Install and run

Use the existing Python 3.13 `.venv`, uv, and the Linuxbrew Node/npm installations:

```sh
uv pip install --python .venv/bin/python -r app/requirements.txt
/home/linuxbrew/.linuxbrew/bin/npm --prefix web install
/home/linuxbrew/.linuxbrew/bin/npm run dev
```

The development command starts Vite and the backend with reload. Vite proxies
`/api` to `http://127.0.0.1:8000`. Either process exiting stops both servers.

```sh
.venv/bin/python -m app ocr samples/test-ebook-simple-page.png
.venv/bin/python -m app ocr samples/test-pdf-simple-page.pdf --output-path output
.venv/bin/python -m app ocr book.pdf --pdf-dpi 200 --llm-workers 2
.venv/bin/python -m app serve --reload
```

CLI stdout contains the completed document directory. Progress and errors go to
stderr: an updating line in terminals, transition lines when redirected. Exit
codes are `0` complete, `2` partial, and `1` failed/interrupted. Partial PDFs retain
failed pages as explicit Markdown placeholders. The web UI remains unchanged;
PDF controls and live progress are currently CLI/API capabilities.

## Configuration and processing

`config.yaml` is resolved relative to the package, independent of the working
directory. Ollama connection/model/timeout settings remain required. The optional
`pipeline` mapping defaults to:

```yaml
pipeline:
  pdf_dpi: 300
  image_workers: 4
  llm_workers: 1
  postprocess_workers: 4
```

All pipeline values must be positive integers. CLI overrides are `--pdf-dpi`,
`--image-workers`, `--llm-workers`, and `--postprocess-workers`. API requests may
override `pdf_dpi`; worker limits are shared server settings. Use one API process:
job state and limits are local to that process and reset on server restart.

```text
inspect → render/prepare [4 processes] → Ollama [1 thread]
        → extract crops/render page Markdown [4 processes] → assemble → ZIP
```

Stages overlap as pages become ready. Bounded scheduling applies backpressure;
page rasters and Markdown are disk-backed. PDFs render at 300 DPI by default,
in color on white, preserving rotation. Input content determines its type.
Existing raster orientation, transparency normalization and format support remain;
multi-frame raster inputs are rejected. Locked PDFs require an unlocked copy.

Each PDF page begins with `<!-- Page 1 -->`, `<!-- Page 2 -->`, etc. Final order
always follows the source PDF. Crops use `page-0001-image-001.png` names; raster
exports retain `image-001.png`. Empty pages keep their page marker. A failed page
does not stop other pages; all-page failure produces no document. Requests are
not retried automatically.

## HTTP API

- `GET /api/health`: application liveness, without contacting Ollama.
- `POST /api/ocr`: multipart `file`, optional `pdf_dpi`; wait for a ZIP attachment.
  Response headers `X-OCR-Status` and `X-OCR-Failed-Pages` identify partial output.
- `POST /api/ocr/stream`: same upload fields; receive Server-Sent Events (SSE).
- `GET /api/ocr/jobs/{job_id}`: current task snapshot and result metadata.
- `GET /api/ocr/jobs/{job_id}/events`: reconnect, optionally with `Last-Event-ID`.
- `GET /api/ocr/jobs/{job_id}/download`: repeatable ZIP download after completion.
- `/docs`: interactive API documentation.

```sh
curl -F 'file=@book.pdf' -F 'pdf_dpi=300' http://127.0.0.1:8000/api/ocr -o book.zip
curl -N -F 'file=@book.pdf' http://127.0.0.1:8000/api/ocr/stream
curl -N -H 'Last-Event-ID: 12' http://127.0.0.1:8000/api/ocr/jobs/JOB_ID/events
```

The stream emits `job.accepted`, `job.planned`, `task.updated`, `page.completed`,
and a terminal `job.completed`, `job.partial`, or `job.failed`. JSON data includes
sequence ID, job ID, timestamp, task details and stage/page counts. Terminal
results include failed pages, download URL and expiry. Streams send heartbeat
comments every 15 seconds; they contain progress rather than LLM tokens.

Jobs continue after disconnect. Save the job ID from the acceptance event to
reconnect. The last 512 events are retained; an older cursor receives a
`job.snapshot` followed by live events. Completed state and files expire one hour
after completion; active downloads pin their files until transmission ends.

Blocking errors use HTTP 400 for invalid input, 502 for recognition/output errors,
504 for Ollama timeout, and 500 for export/infrastructure failures. Invalid form
fields use 422. Streaming failures after acceptance are terminal SSE events.
Unknown/expired jobs return 404; unfinished or failed downloads return 409.

Complete sample streams, polling JSON, event payloads and browser parsing:
[API event examples](.memory/api-events.md).

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
/home/linuxbrew/.linuxbrew/bin/npm --prefix web run typecheck
/home/linuxbrew/.linuxbrew/bin/npm --prefix web run build
```

Python dependencies live only in `app/requirements.txt`. Implementation contracts
and entry points are documented in [.memory/pdf-pipeline.md](.memory/pdf-pipeline.md)
and [.memory/llm-output-processing.md](.memory/llm-output-processing.md).
