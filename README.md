# ocr-app

Document OCR application skeleton with a Python CLI, FastAPI backend, and
TypeScript/Vite frontend. Image/PDF conversion and Ollama inference are explicit
stubs: OCR currently fails with a clear message (CLI exit 1 or HTTP 501).

## Install and run

Use the existing Python 3.13 `.venv`, uv, Node.js 20.19+ (20.x) or 22.12+,
and npm 11.x. From the repository root:

```sh
uv pip install --python .venv/bin/python -r app/requirements.txt
npm --prefix web install
npm run dev
```

`npm run dev` also works inside `web/`. It starts Vite and the backend with
Python reload enabled. Vite prints its URL; the backend listens on
http://127.0.0.1:8000. Ctrl+C or either process exiting stops both servers.
Vite proxies `/api` to the backend, preserving the path, and restricts file
serving to `web/`.

Run the CLI or backend independently from the repository root:

```sh
.venv/bin/python -m app --help
.venv/bin/python -m app ocr samples/document.pdf
.venv/bin/python -m app serve --reload
```

Replace the sample path with an existing file. Omit `--reload` to disable
source reload. Successful OCR will eventually print JSON containing an ordered
`pages` list with `page_number` (one-based) and `text`.

## Configuration and HTTP API

Edit the existing `config.yaml` to set `ollama.base_url`, `ollama.model`, and
`ollama.timeout_seconds`. Defaults are `http://localhost:11434`, `deepseek-ocr`,
and 120 seconds. The config path is resolved relative to the Python package,
not the working directory. Invalid settings fail at CLI invocation or API startup.
The configured Ollama client is created without making a network request.

- `GET /api/health` returns `{"status":"ok"}` without checking Ollama.
- `POST /api/ocr` accepts one multipart field named `file` and currently returns
  HTTP 501 with an explicit conversion-not-implemented message.
- Interactive API documentation is available at `/docs` on the backend.

```sh
curl http://127.0.0.1:8000/api/health
curl -F 'file=@samples/document.pdf' http://127.0.0.1:8000/api/ocr
```

## Architecture and planned processing

`app` is an importable package. `__main__` and `api` adapt CLI arguments and
HTTP uploads to the same synchronous `OCRService.ocr(file, filename)` method.
`config` loads settings; `processing.iter_pages` and
`ollama_client.OllamaOCRClient.recognize` are the conversion and inference stubs.
The service preserves page order, and client resources are closed at shutdown.

The future conversion contract is:

- Pillow handles common raster formats (including JPEG, PNG, WebP, GIF, BMP,
  and TIFF); pillow-heif adds HEIC/HEIF. Support depends on installed decoders;
  unsupported inputs must fail clearly. SVG and camera RAW are excluded.
- Correct EXIF orientation, flatten transparency onto white, and encode RGB PNG.
  Process every TIFF page and the first frame of animated images.
- pypdfium2 renders PDF pages sequentially at 200 DPI and releases page resources
  promptly. All PDFium calls must share a process-wide lock because
  [PDFium is not thread-safe](https://pypdfium2.readthedocs.io/en/stable/python_api.html).
- Send one PNG per Ollama request using `deepseek-ocr`, prompt `Free OCR.`, and
  streaming disabled. [DeepSeek-OCR requires Ollama 0.13.0 or later](https://ollama.com/library/deepseek-ocr).
  Provision the model on the configured Ollama instance before implementing inference.

Conversion, inference, queues, persistence, and upload UI remain deferred.
Python dependencies live only in `app/requirements.txt`; no additional Python
configuration or lockfiles are used.

## Checks and frontend build

```sh
.venv/bin/python -m unittest discover -s tests -v
npm --prefix web run typecheck
npm --prefix web run build
npm --prefix web run preview
```

The production frontend build is written to `web/dist/`. Preview serves that
build; start the backend separately for API requests.
