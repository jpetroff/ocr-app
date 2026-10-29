# OCR providers

Entry points: `app.config.load_settings`, `app.ocr_client.create_ocr_client`,
`app.service.create_service`, `app.pipeline.Pipeline._recognize`.

```yaml
provider: openailike # ollama if omitted

ollama:
  base_url: http://ollama.intranet
  model: deepseek-ocr:3b
  timeout_seconds: 360

openailike:
  base_url: http://llama.intranet # also accepts /v1 and trailing slash
  model: deepseek-ocr
  timeout: 360
  prompt: '<|grounding|>Convert the document to markdown.' # optional default
```

- Only the selected provider mapping is required/validated. Shared `pipeline`
  validation and CLI overrides remain active. Unknown providers fail configuration.
- `OCRSettings`: active URL/model/`timeout_seconds`/pipeline/provider/prompt.
  `OllamaSettings` remains an alias; positional fields and Ollama defaults survive.
- `OCRClient`: `recognize(bytes) -> str`, `close()`. Both service paths use the
  factory. Pipeline clients belong to LLM executor threads and close at shutdown.

```text
ollama     -> OllamaOCRClient -> SDK generate: /api/generate
openailike -> OpenAILikeOCRClient -> httpx POST /v1/chat/completions
             model + stream:false
             user.content: [image_url(data:<actual MIME>;base64,...), text(prompt)]
             -> choices[0].message.content -> raw text -> existing postprocessing
```

- Prepared raster filenames can end in `.png` while preserving JPEG/WebP bytes.
  MIME comes from Pillow's detected format, not the filename. Bytes are unchanged.
- Prompt strings preserve whitespace. Empty/non-string prompts fail configuration.
  Prompt choice is model-specific; grounded Markdown remains the DeepSeek default.
- Empty response strings are valid. Malformed JSON/content -> `OCRBackendError`.
  HTTP/network errors -> `OCRBackendError`; timeout -> `OCRTimeoutError`.
  Existing blocking API mappings remain 502/504; CLI failure exit remains 1.
- No inference at construction/startup/health. No retries, fallback, model
  discovery/download, authentication, or per-request provider selection.
- Direct HTTP dependency: `httpx` in `app/requirements.txt`; no OpenAI SDK.

Request reference:
[Using OCR models with llama.cpp](https://huggingface.co/blog/ggml-org/using-ocr-models-with-llama-cpp).

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -q
```

- `tests/test_openailike_client.py`: config, exact payload/MIME/bytes, root and
  `/v1` URLs, errors, client cleanup, health, CLI raw/crop exports, PDF streaming.
- `tests/provider_helpers.py::select_provider`: explicit CLI/API settings;
  local provider selection cannot redirect mocked integration tests.
- HTTP tests use `httpx.MockTransport`; no live server is needed.
- 2026-10-29: full Python suite passed, 85 tests. No live llama.cpp request.
