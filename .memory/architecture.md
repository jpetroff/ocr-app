# OCR application architecture

## Status and boundaries

The application is a runnable Python skeleton with CLI and FastAPI adapters
around one shared synchronous OCR service. The TypeScript web app uses Vite.
Image/PDF conversion and Ollama inference are explicit stubs; no OCR text is
currently produced. Queues, persistence, and an upload UI remain deferred.

## Modules and data flow

All Python application modules belong to the importable `app` package.

| Module | Responsibility |
| --- | --- |
| `__main__` | Parse `ocr FILE` and `serve [--reload]` with standard-library argparse. |
| `config` | Read YAML, validate settings, and return immutable `OllamaSettings`. |
| `service` | Own the shared OCR orchestration, result dataclasses, and client lifecycle. |
| `processing` | Define numbered PNG pages and the conversion stub. |
| `ollama_client` | Construct the configured Ollama client and expose the inference stub. |
| `api` | Define the FastAPI lifespan, service dependency, and HTTP routes. |
| `errors` | Define `OCRNotImplementedError` for both adapters. |

```text
CLI: file opened in binary mode ─┐
                               ├─> OCRService.ocr(file, filename)
API: UploadFile.file ───────────┘       │
                                       ├─> iter_pages(file, filename) [stub]
                                       │      yields ImagePage(page_number, png_bytes)
                                       ├─> client.recognize(png_bytes) [stub]
                                       │      called sequentially for each page
                                       └─> OCRResult(pages=[OCRPage(page_number, text), ...])
```

The shared entry point is `OCRService.ocr(file: BinaryIO, filename: str) -> OCRResult`.
Page numbers are one-based; results preserve source order. The intended success
output is JSON with a `pages` array containing `page_number` and `text`. The CLI
serializes dataclasses; FastAPI uses `OCRResult` as its response model.

Currently, `iter_pages` immediately raises `OCRNotImplementedError`, so inference
is never reached. `recognize` independently raises the same error if called.
The adapters expose these failures as CLI stderr plus exit code 1, or HTTP 501
with a JSON `detail` message. Missing CLI files also fail with exit code 1.

## Configuration and lifecycle

The repository-root `config.yaml` is the only application configuration file.
Its location is resolved from `config.py`, independently of the working directory.
Use the file's current values as authoritative; its Ollama instance, model tag,
and timeout are user-editable and must not be reset to example values.

Required settings are:

| Key | Validation |
| --- | --- |
| `ollama.base_url` | HTTP or HTTPS URL with a hostname and valid optional port. |
| `ollama.model` | Non-empty string, normally a `deepseek-ocr` model tag. |
| `ollama.timeout_seconds` | Finite positive number; booleans are rejected. |

`yaml.safe_load` reads the configuration. Missing, malformed, or invalid settings
raise `ConfigError`; there are no implicit configuration defaults or environment
overrides. The CLI validates settings before running either command. FastAPI
loads them during startup.

`create_service(settings)` constructs one Ollama adapter and closes its client
in a `finally` block. CLI OCR uses this context for one invocation. FastAPI uses
it for the application lifespan and stores the service in `app.state.ocr_service`.
Client construction makes no Ollama requests or model downloads, so server
startup and health checks work without a reachable Ollama instance.

## HTTP and development startup

| Endpoint | Behavior |
| --- | --- |
| `GET /api/health` | Returns `{"status":"ok"}` for application liveness only. |
| `POST /api/ocr` | Accepts one multipart field named `file`; currently returns HTTP 501. |
| `GET /docs` | FastAPI's interactive API documentation. |

The OCR route is synchronous: FastAPI runs it in its thread pool, keeping future
blocking conversion and inference off the event loop. Missing upload fields
receive FastAPI's HTTP 422 validation response.

From the repository root:

```sh
uv pip install --python .venv/bin/python -r app/requirements.txt
npm --prefix web install

.venv/bin/python -m app ocr samples/document.pdf
.venv/bin/python -m app serve --reload

# Start both frontend and backend instead:
npm run dev
```

Use an existing input file for the CLI. The server command invokes Uvicorn with
`app.api:app` on `127.0.0.1:8000`. Reload is opt-in and watches the Python package.

The root npm `dev` script delegates to the web package. Inside `web`, `dev` uses
`concurrently --kill-others` to run `dev:web` (Vite) and `dev:api` (the Python
server with reload). Running `npm run dev` from either directory starts both.
Ctrl+C or either child exiting stops the other server and the reload child.

The existing Vite configuration proxies `/api` to `http://127.0.0.1:8000` without
rewriting paths and restricts served files to `web/`. The backend starts
separately when previewing the frontend production build in `web/dist/`.

## Future conversion and inference contracts

- Pillow decodes common raster images; pillow-heif adds HEIC/HEIF. Actual format
  coverage depends on installed decoders. Unsupported files must fail clearly;
  SVG and camera RAW are excluded.
- Correct EXIF orientation, flatten transparency onto white, and encode RGB PNG.
  Process every TIFF page, but only the first frame of animated images.
- pypdfium2 renders PDF pages sequentially at 200 DPI. Yield pages individually
  and promptly close bitmap, page, and document resources. Protect all PDFium
  calls across requests with a process-wide lock because PDFium is not thread-safe.
- Send one PNG per Ollama request with the configured model, prompt `Free OCR.`,
  and streaming disabled. Preserve page order. DeepSeek-OCR requires Ollama
  0.13.0 or later and a model provisioned on the configured instance.

These contracts are documented beside the stubs; conversion, PDFium locking,
and network inference have not yet been implemented.

## Dependencies and verification

Use uv with the existing Python 3.13 `.venv`. Keep Python dependencies exclusively
in `app/requirements.txt`: FastAPI, Uvicorn with standard extras,
python-multipart, PyYAML, ollama, Pillow, pillow-heif, and pypdfium2. Do not add
Python project manifests, lockfiles, or additional configuration files.
Web dependencies use the existing web package manifest and npm lockfile.
Development requires Node.js 20.19+ (20.x) or 22.12+ and npm 11.x on PATH.

```sh
.venv/bin/python -m unittest discover -s tests -v
npm --prefix web run typecheck
npm --prefix web run build
```

The Python tests cover configuration validation and path resolution, ordered
service calls, client cleanup, explicit stubs, CLI failures, API responses, and
invalid startup configuration. Live development startup, proxy requests, and
shutdown after Ctrl+C or either server exiting were verified during bootstrap.
