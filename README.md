# ocr-app

Single-image OCR with a Python CLI, FastAPI backend, and TypeScript/Vite
frontend. The backend sends images to the configured Ollama DeepSeek-OCR model
and returns raw generated text. PDF and multi-frame inputs are deferred.

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
.venv/bin/python -m app ocr samples/test-ebook-simple-page.png
.venv/bin/python -m app serve --reload
```

Replace the sample path with an existing file. Omit `--reload` to disable
source reload. Successful OCR writes raw model text to stdout, preserving
whitespace and adding no newline. The API returns the same text as `text/plain`.

## Configuration and HTTP API

Edit the existing `config.yaml` to set `ollama.base_url`, `ollama.model`, and
`ollama.timeout_seconds`. All settings are required; there are no implicit
defaults. The config path is resolved relative to the Python package,
not the working directory. Invalid settings fail at CLI invocation or API startup.
The configured Ollama client is created without making a network request.

- `GET /api/health` returns `{"status":"ok"}` without checking Ollama.
- `POST /api/ocr` accepts one multipart field named `file` and returns raw
  generated text as UTF-8 `text/plain`.
- Invalid images return HTTP 400; Ollama failures return 502; timeouts return 504.
- Interactive API documentation is available at `/docs` on the backend.

```sh
curl http://127.0.0.1:8000/api/health
curl -F 'file=@samples/test-ebook-simple-page.png' http://127.0.0.1:8000/api/ocr
```

## Architecture and processing

`OCRService.ocr(file, filename) -> str` is shared by CLI and API.
`processing.prepare_image` validates content and preserves supported PNG/JPEG/WebP
bytes when normalization is unnecessary. `processing.convert_image` converts
other supported raster images to RGB PNG, applies EXIF orientation, and flattens
transparency onto white. Multi-frame images are rejected.

`ollama_client.OllamaOCRClient.recognize` sends one image with prompt `Free OCR.`
and streaming disabled. Output whitespace, Unicode, and markup are preserved.
Client resources are closed at shutdown.

Implementation contracts and verification are in
[.memory/architecture.md](.memory/architecture.md). Python dependencies live only
in `app/requirements.txt`.

## Checks and frontend build

```sh
.venv/bin/python -m unittest discover -s tests -v
npm --prefix web run typecheck
npm --prefix web run build
npm --prefix web run preview
```

The production frontend build is written to `web/dist/`. Preview serves that
build; start the backend separately for API requests.
