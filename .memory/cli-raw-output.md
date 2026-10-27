# CLI raw LLM output

Entry points: `app.__main__.main`, `run_ocr`; internal retention:
`Pipeline.submit(..., save_raw=True)`, `Job.save_raw`, `postprocess_page`.

```bash
.venv/bin/python -m app ocr scan.png --output ./output --save-raw
.venv/bin/python -m app ocr book.pdf --output-path ./output --save-raw
```

```text
output/<unique-document-folder>/
  document.md             # successful/partial processing only
  raw.txt                 # image response, exact UTF-8 text
  page-0001-raw.txt        # PDF responses use source page numbers
  page-0002-raw.txt
```

- `--save-raw` belongs to `ocr` only; default false. No config/API option.
- `--output` aliases `--output-path`; both select the output parent.
- Retain `raw-N.txt` through successful and failed post-processing. CLI copies
  responses before workspace cleanup, alongside Markdown/crops when available.
- Print one output folder on stdout, including raw-only failures. Preserve
  exit codes: success 0, partial 2, fatal 1; errors/progress stay on stderr.
- No returned response (input/backend failure): no raw file/output folder.
- No raw normalization, grounding removal, page separators, or newline changes.
- Flag omitted: existing export and cleanup behavior. API ZIPs unchanged.

Verification: `tests/test_app.py::AdapterTests` raw image success/empty/malformed
response and backend failure; `tests/test_pipeline.py::APIAndCLITests` PDF
partial/all-page post-processing failure. Full unittest suite: 64 passing.
