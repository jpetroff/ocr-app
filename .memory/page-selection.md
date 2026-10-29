# Page selection

Entry points: `app.__main__.page_ranges`, `app.api.api_page_selection`,
`app.page_selection.PageSelection.resolve`, `app.pipeline.Pipeline._plan`.

```sh
.venv/bin/python -m app ocr book.pdf --include '1-23,45-44,50,79-'
.venv/bin/python -m app ocr book.pdf --exclude=-5,8-10
curl -F 'file=@book.pdf' -F 'include=1' -F 'include=3' \
  http://127.0.0.1:8000/api/ocr -o selected.zip
curl -N -F 'file=@book.pdf' -F 'exclude=2' -F 'exclude=4' \
  http://127.0.0.1:8000/api/ocr/stream
```

| CLI token | Inclusive pages |
| --- | --- |
| `50` | 50 |
| `1-23` | 1 through 23 |
| `45-44` | 44, 45 |
| `79-` | 79 through last page |
| `-23` | 1 through 23 |
| `-` | all pages |

- One-based page numbers. `--include` / `--exclude` are mutually exclusive;
  neither supplied selects all pages. Use `--include=...` / `--exclude=...`
  when a comma-separated value begins with `-` (argparse option handling).
- API: both upload endpoints accept repeated `include` or `exclude` multipart
  fields, each a positive integer. No range strings or comma-separated fields.
  Fields appear as integer arrays in `/openapi.json` and `/docs`.
- Syntax, nonpositive integers and API field conflicts: CLI argument error / HTTP
  422 before submission. Explicit endpoints above document length or exclusion
  of every page: CLI exit 1 / blocking HTTP 400 / terminal `job.failed` SSE.
- `PageRange` stores endpoints; expansion waits for input inspection and bounds
  checks. `PageSelection.resolve` deduplicates and sorts in source order.
- `_plan` creates image/LLM/postprocess tasks only for selected pages. Assembly
  dependencies and completion counts use selected pages. Omitted pages produce
  no OCR calls, placeholders, crops or raw responses. Raster inputs have page 1.
- Source page numbers persist in task IDs, Markdown markers, crop names and
  `--save-raw` exports. Partial failures apply only to selected pages.
- `stats.pages_total`: selected count; `stats.pages_source`: source count.
  Both zero before inspection. `job.planned` includes `pages` (selected count),
  `source_pages`, `selected_pages` (ordered source numbers), `is_pdf`.
- No dependencies added. Verification: `tests/test_page_selection.py` plus the
  full unittest suite; real PDF workers, exports, polling and streaming covered.
