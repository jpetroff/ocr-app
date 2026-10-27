# LLM output processing

Pipeline entry points: `app.pipeline.Pipeline._recognize`, `postprocess_page`.
Legacy in-process helper: `app.service.OCRService.ocr(file, filename) -> OCRDocument`.
Scheduling, PDF assembly and streaming: [pdf-pipeline.md](pdf-pipeline.md).

```python
image_bytes = prepare_image(file, filename)
raw_text = client.recognize(image_bytes)
document = build_document(raw_text, image_bytes)
```

```text
Ollama response.response: str
  -> parse_document(text, width, height)
  -> ordered blocks + normalized/pixel boxes
  -> build_document: image blocks -> PNG bytes
  -> render_markdown -> page-N/page.md + crop files
  -> assemble_document (source page order) -> archive_document
       CLI: copy completed directory to --output-path
       blocking API: ZIP -> TemporaryZipResponse -> cleanup
       streaming API: SSE result -> retained ZIP -> JobZipResponse -> expiry
```

## Recognition and parsing

Source: [app/ollama_client.py](../app/ollama_client.py),
[app/document.py](../app/document.py).

- `OllamaOCRClient.recognize` uses prompt
  `<|grounding|>Convert the document to markdown.` with `stream=False`.
  Returns `response.response` unchanged; rejects non-string responses.
- `parse_document` recognizes both marker forms:

  ```text
  image[[500,0,999,999]]
  <|ref|>image<|/ref|><|det|>[[500,0,999,999]]<|/det|>
  ```

- Simplified markers start a line, optionally indented. Labels match
  `[A-Za-z_][A-Za-z0-9_-]*`; native labels are nonempty and trimmed.
- Coordinates are JSON arrays. Multiple boxes:
  `image[[0,0,499,999],[500,0,999,999]]`.
- Text after a marker through the next marker/end becomes that block's `content`,
  including leading/trailing newlines. Markers become metadata.
- Keep model order, unknown labels, empty blocks, Markdown, HTML, and Unicode.
  Text before the first marker becomes an ungrounded `text` block. With no
  markers, all text becomes one block; `""` produces no blocks.

## Boxes and binary crops

Search: `BoundingBox`, `_boxes`, `build_document`, `BlockImage`.

```text
OCRDocument(width, height, blocks: list[OCRBlock])
  OCRBlock(label, bounding_boxes, content, images)
    BoundingBox(normalized, pixels)
    BlockImage(bounding_box_index, filename, data: bytes)
```

- `_boxes`: nonempty box list; each box has four integers in 0–999;
  booleans/floats rejected; require `x1 < x2`, `y1 < y2`.
- Convert coordinates independently per axis:

  ```python
  left, right = int(x1 / 999 * width), int(x2 / 999 * width)
  top, bottom = int(y1 / 999 * height), int(y2 / 999 * height)
  ```

- `build_document` opens the prepared inference image once for extraction.
  EXIF rotation/format normalization already happened in `prepare_image`.
- Only `label == "image"` crops pixels. `image_caption` remains content.
  Each box produces one PNG; reject zero-width/height pixel crops.
- Number images globally in block/box order: `image-001.png`, `image-002.png`.
  `bounding_box_index` is zero-based within its block.
- Store encoded PNG `bytes`, not base64 or decoded rasters. Close source/crops
  after extraction. SDK base64 transport encoding is independent of this storage.

Example, prepared image size **100 × 60**:

```text
image[[500,0,999,999]]
image_caption[[0,0,999,999]]
<center>café</center>
```

```text
blocks[0]: label="image", content="\n"
  bounding_boxes[0]: normalized=(500,0,999,999), pixels=(50,0,100,60)
  images[0]: bounding_box_index=0, filename="image-001.png"
             data=<PNG bytes; 50 × 60 pixels>
blocks[1]: label="image_caption", content="\n<center>café</center>"
  bounding_boxes[0]: normalized=(0,0,999,999), pixels=(0,0,100,60)
  images=[]
```

## Rendering and delivery

Source: [app/export.py](../app/export.py), [app/api.py](../app/api.py),
[app/__main__.py](../app/__main__.py).

- `render_markdown`: append each block's image references, then nonempty content;
  join pieces using `"\n\n"`. Content interiors remain unchanged; existing
  boundary newlines can produce extra blank lines.
- Image references use `![Image](image-001.png)`. Captions keep their own position;
  no semantic heading conversion, caption attachment, or box metadata in Markdown.
- `postprocess_page`: write per-page Markdown/crops; release inference raster and
  raw text after success. PDF crop names use `page-0001-image-001.png`; raster
  exports retain `image-001.png`. All paths in Markdown remain relative.
- `assemble_document`: trim page-edge CR/LF; collapse 3+ consecutive newlines
  to `"\n\n"` (LF or CRLF). Single line breaks and other whitespace remain.
  Example: `"\nFirst\n\n\nSecond\n"` -> `"First\n\nSecond"`.
  Combine pages numerically; separate PDF comments/content with `"\n\n"`,
  including empty pages; retain failed-page placeholders. UTF-8 `document.md` and
  adjacent PNGs in one unique directory. `export_document` remains available as
  the single-image export helper.
- CLI `--output-path` selects the parent, default cwd; stdout prints completed
  child path, stderr displays task progress. Partial output exits 2.
- API `POST /api/ocr`: same pipeline under private `/tmp/ocr-*`; ZIP one top-level
  document directory, return `application/zip` attachment from disk. Partial
  response headers: `X-OCR-Status`, `X-OCR-Failed-Pages`.
- `TemporaryZipResponse.__call__`: keep files through transmission, clean in
  `finally` on completion/error/cancellation. Disable `pathsend` so transmission
  finishes before cleanup. Generation failures clean the workspace immediately.
- Streaming jobs retain completed files for one hour. `JobZipResponse` pins files
  through transmission; `Pipeline.expire` removes unpinned expired workspaces.
  Disconnecting an SSE subscriber does not cancel recognition.

## Errors and verification

| Condition | Error | API |
| --- | --- | --- |
| Invalid response/non-string text | `OCRBackendError` | 502 |
| Recognized malformed grounding, invalid boxes, stray native tokens, empty image crop | `OCROutputError` (`OCRBackendError`) | 502 |
| Backend timeout | `OCRTimeoutError` | 504 |
| Filesystem/export failure | `OSError` | 500 |

The table describes blocking failure responses. For multi-page PDFs, expected
page errors become placeholders and the job continues; any successful page allows
partial output. All-page/infrastructure failure publishes no document. Streaming
jobs report failures via SSE. CLI fatal errors write stderr and exit 1; partial
output prints its path and exits 2.

- `tests/test_document.py`: supplied `tests/fixtures/double_drawer.txt` -> six
  blocks, one PNG; native/simplified/mixed markers; exact content; pixel/EXIF checks.
- `tests/test_export.py`: CLI/ZIP file equivalence, relative references, unique
  directories, failed writes, workspace lifetime, cancelled/disconnected responses.
- `tests/test_ollama_client.py`: actual SDK over mock HTTP transport; grounding
  prompt, raw-string preservation, conversion through CLI/API.

See [architecture.md](architecture.md) for input validation and service lifecycle.
