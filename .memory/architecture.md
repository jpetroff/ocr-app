# OCR implementation

Focused walkthrough: [LLM output processing](llm-output-processing.md).

## Entry points and data flow

```text
python -m app ocr FILE [--output-path DIRECTORY] ─┐
POST /api/ocr (file) ────────────────────────────┴─> OCRService.ocr -> OCRDocument
                              |
                              prepare_image(file, filename)
                                ├─ validated PNG/JPEG/WebP -> original bytes
                                └─ normalization needed -> convert_image -> RGB PNG
                              |
                              OllamaOCRClient.recognize(image_bytes)
                              |  generate(model=settings.model,
                              |           prompt="<|grounding|>Convert the document to markdown.",
                              |           images=[image_bytes], stream=False)
                              |
                              response.response (raw text)
                              |
                              app.document.build_document(text, image_bytes)
                              |  parse_document -> blocks + boxes
                              |  exact image labels -> PNG crop bytes
                              |
                              app.export.export_document(document, parent, filename)
                                ├─ CLI: new directory; print absolute path
                                └─ API: /tmp workspace -> archive_document -> TemporaryZipResponse
```

Raw text remains the Ollama client contract. Service/adapters return structured
documents and exports; no raw-output compatibility mode.

## Block document and grounding

Entry points: app.document.parse_document, build_document.

```text
OCRDocument(width, height, blocks: list[OCRBlock])
  OCRBlock(label, bounding_boxes, content, images)
    BoundingBox(normalized=(x1,y1,x2,y2), pixels=(left,top,right,bottom))
    BlockImage(bounding_box_index, filename, data: bytes)
```

- Parse line-start `label[[x1,y1,x2,y2], ...]` and native
  `<|ref|>label<|/ref|><|det|>[[x1,y1,x2,y2], ...]<|/det|>`.
- JSON coordinate arrays only; four integers per box, 0–999, x1 < x2, y1 < y2.
- Pixels: `int(x / 999 * width)`, `int(y / 999 * height)`.
- Preserve block order, unknown labels, following content verbatim, Markdown,
  HTML captions, Unicode, and empty image blocks. Ungrounded text -> text block;
  empty output -> empty blocks.
- Only exact `image` labels produce crops. Decode the prepared inference image
  once for extraction; encode each crop as PNG bytes. Close source/crop objects.
- EXIF orientation and normalization precede inference and cropping.
- Malformed grounding/coordinates or empty image pixel crops -> OCROutputError.
- No base64 images in OCRDocument; no retained source raster or duplicate raw text.

## Export layout and ownership

Entry points: app.export.render_markdown, export_document, archive_document;
app.api.TemporaryZipResponse.

```text
<safe-input-stem>-<unique-suffix>/
  document.md
  image-001.png
  image-002.png
```

- `export_document` creates the parent and a unique child with `mkdtemp`;
  sanitize filenames to an ASCII stem, default `document`, at most 80 characters.
- Image numbering follows block/box order. Markdown uses `![Image](image-001.png)`;
  preserve following content and separate pieces with blank lines. Captions stay
  separate; boxes remain internal. Plain-text-only exports preserve exact text.
- Remove incomplete child directories on any export failure; never overwrite.
- CLI `--output-path` means parent directory; default cwd. Success prints the
  completed child's absolute path plus newline.
- API accepts only multipart `file`. Create a private `/tmp/ocr-*` workspace,
  export a document child, ZIP files under that single directory, send attachment
  `application/zip`. Archive creation/response use disk files, not a ZIP byte buffer.
- Response owns TemporaryDirectory. Cleanup in `finally` after success, send
  failure, disconnect, or cancellation; generation failure cleans up immediately.
- Disable response `pathsend`: send file chunks before deleting the workspace.

## Image processing

Entry points: app.processing.prepare_image, convert_image, _open_image.

| Condition | Behavior |
| --- | --- |
| PNG/JPEG/WebP, RGB or L, no transparency, orientation 1 | Preserve original bytes. |
| BMP/GIF/TIFF/HEIF/AVIF or normalization required | Decode using installed Pillow codecs; EXIF transpose; alpha over white; RGB PNG. |
| Empty, invalid, truncated, unsupported format | OCRInputError. |
| Multiple frames/pages | OCRInputError; supply one image. |

Inspect decoded content, independent of filename or upload content type.
Read BinaryIO from its current position; caller retains stream ownership.
Register pillow-heif once with thumbnails disabled. Close decoded images.
PDF, SVG, camera RAW, and multi-page processing remain outside this contract.

## Ollama and lifecycle

- app.config.load_settings reads repository-root config.yaml independently of cwd.
- Preserve configured base_url, model tag, and timeout_seconds; no implicit defaults.
- Current model tag: deepseek-ocr:3b.
- create_service owns and closes the SDK client, including on failures.
- Client construction, API startup, and GET /api/health make no Ollama requests.
- SDK handles base64 encoding; one generate request per image.
- No automatic model downloads or retries. SDK output stays raw; service parses it.
- HTTP/connection/server errors -> OCRBackendError.
- HTTP transport timeout -> OCRTimeoutError.
- Invalid response or missing generated text -> OCRBackendError.
- The synchronous OCR endpoint runs in FastAPI's thread pool.

## Adapter behavior

| Outcome | CLI | POST /api/ocr |
| --- | --- | --- |
| Success | Export directory path; exit 0 | 200, application/zip attachment |
| Invalid input | stderr; exit 1 | 400, JSON detail |
| Backend failure | stderr; exit 1 | 502, JSON detail |
| Invalid OCR grounding/crop | stderr; exit 1 | 502, JSON detail |
| Backend timeout | stderr; exit 1 | 504, JSON detail |
| Filesystem/export failure | stderr; exit 1 | 500, JSON detail |
| Missing multipart file | N/A | 422 |

CLI entry: app.__main__.main. API entries: app.api.ocr and lifespan.
GET /api/health returns {"status":"ok"}; /docs exposes OpenAPI.
ConfigError prevents invalid CLI invocation/API startup.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app ocr samples/test-ebook-simple-page.png --output-path /tmp/ocr-output
curl -F 'file=@samples/test-ebook-simple-page.png' http://127.0.0.1:8000/api/ocr -o /tmp/ocr-result.zip
```

45 tests pass without live Ollama:
- tests/test_processing.py: format detection, pass-through, fallback formats,
  HEIF, alpha/palette transparency, EXIF, invalid and multi-frame input.
- tests/test_ollama_client.py: actual SDK over httpx.MockTransport; base64,
  configured model/timeout, nonstreaming request, raw/empty text, upstream errors,
  CLI/API through conversion and SDK.
- tests/test_app.py: service calls, lifecycle, config, adapter output and errors.
- tests/test_document.py: six-block double_drawer fixture, native/simplified/mixed
  markers, multiple boxes, unknown labels, empty/plain text, invalid grounding,
  binary crops, non-square pixel scaling, boundary pixels, EXIF-aligned crops.
- tests/test_export.py: safe unique directories, identical CLI/ZIP files,
  relative references, OpenAPI ZIP content, failed-write cleanup, HTTP 500,
  workspace lifetime, failed/cancelled transmission, ASGI disconnect cleanup.

No dependencies added; existing app/requirements.txt remains authoritative.
Existing Starlette TestClient emits an httpx deprecation warning.

## References

- [Ollama Python](https://github.com/ollama/ollama-python): generate and SDK errors.
- [Ollama vision](https://docs.ollama.com/capabilities/vision): byte input and SDK encoding.
- [DeepSeek-OCR](https://ollama.com/library/deepseek-ocr): model and prompt.
- [DeepSeek reference extraction](https://huggingface.co/deepseek-ai/DeepSeek-OCR/blob/main/modeling_deepseekocr.py): 0–999 grounding coordinates.
- [Pillow EXIF transpose](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose).
- [HEIF plugin registration](https://pillow-heif.readthedocs.io/en/latest/pillow-plugin.html).
