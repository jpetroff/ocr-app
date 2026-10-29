"""Config, HTTP transport and adapter coverage for provider selection."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
import httpx
from PIL import Image

from app.__main__ import main
from app.api import app
from app.config import ConfigError, DEFAULT_OCR_PROMPT, OCRSettings, OllamaSettings, PipelineSettings, load_settings
from app.errors import OCRBackendError, OCRInputError, OCRTimeoutError
from app.ocr_client import create_ocr_client
from app.ollama_client import OllamaOCRClient
from app.openailike_client import OpenAILikeOCRClient
from app.service import create_service
from provider_helpers import select_provider
from test_pipeline import pdf_bytes, parse_events
from test_processing import encoded_image


SETTINGS = OCRSettings("http://llama.test", "deepseek-ocr", 45, provider="openailike")


def mock_client(handler):
    client_class = httpx.Client
    return patch("app.openailike_client.httpx.Client", side_effect=lambda **kwargs:
                 client_class(transport=httpx.MockTransport(handler), **kwargs))


class ProviderConfigTests(unittest.TestCase):
    def load(self, document):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "config.yaml")
            path.write_text(document, encoding="utf-8")
            return load_settings(path)

    def test_selection_legacy_defaults_and_inactive_provider_independence(self):
        ollama = "ollama: {base_url: http://ollama.test, model: ocr, timeout_seconds: 12}\n"
        openailike = "openailike: {base_url: http://llama.test/v1/, model: deepseek-ocr, timeout: 45}\n"
        for document in (ollama, "provider: ollama\n" + ollama + "openailike: []\n"):
            with self.subTest(document=document):
                settings = self.load(document)
                self.assertEqual(settings.provider, "ollama")
                self.assertEqual(settings.timeout_seconds, 12.0)
        settings = self.load("provider: openailike\n" + openailike + "ollama: []\n"
                             "pipeline: {pdf_dpi: 144, llm_workers: 2}\n")
        self.assertEqual(settings, OCRSettings("http://llama.test/v1/", "deepseek-ocr", 45,
                                              PipelineSettings(144, 4, 2, 4), "openailike"))
        self.assertEqual(settings.prompt, DEFAULT_OCR_PROMPT)
        self.assertEqual(self.load("provider: openailike\n" + openailike).provider, "openailike")
        self.assertIs(OllamaSettings, OCRSettings)
        self.assertEqual(OllamaSettings("http://unused", "ocr", 1).provider, "ollama")

    def test_custom_prompt_is_preserved(self):
        settings = self.load("provider: openailike\nopenailike:\n  base_url: http://llama.test\n"
                             "  model: ocr\n  timeout: 12\n  prompt: ' OCR markdown '\n")
        self.assertEqual(settings.prompt, " OCR markdown ")

    def test_invalid_selection_and_selected_provider_values(self):
        valid = "provider: openailike\nopenailike: {base_url: http://llama.test, model: ocr, timeout: 45}\n"
        cases = [
            ("provider: unknown\n", "provider"), ("provider: null\n", "provider"),
            ("provider: []\n", "provider"), ("provider: openailike\n", "openailike"),
            ("provider: openailike\nopenailike: []\n", "openailike"),
        ]
        for key, values in {
            "base_url": ("ftp://localhost", "http://localhost:bad", "''", "null"),
            "model": ("''", "null", "42"),
            "timeout": ("0", "-1", "true", "null", "'45'", ".inf", ".nan"),
        }.items():
            original = {"base_url": "http://llama.test", "model": "ocr", "timeout": "45"}[key]
            cases.extend((valid.replace(f"{key}: {original}", f"{key}: {value}"), f"openailike.{key}")
                         for value in values)
        cases.extend((valid.replace("timeout: 45", f"timeout: 45, prompt: {value}"), "openailike.prompt")
                     for value in ("''", "'  '", "null", "12", "[]"))
        cases.extend((valid + f"pipeline: {value}\n", "pipeline")
                     for value in ("[]", "{pdf_dpi: 0}", "{unknown: 1}"))
        for document, field in cases:
            with self.subTest(document=document), self.assertRaisesRegex(ConfigError, field):
                self.load(document)


class OpenAILikeClientTests(unittest.TestCase):
    def test_endpoints_payload_mime_timeout_and_raw_text(self):
        raw = '  # OCR café\n"quoted"\r\n\n'
        for suffix in ("", "/", "/v1", "/v1/"):
            for image_format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
                with self.subTest(suffix=suffix, image_format=image_format):
                    image_bytes = encoded_image(image_format)
                    settings = replace(SETTINGS, base_url=SETTINGS.base_url + suffix, prompt="OCR markdown")

                    def handler(request):
                        self.assertEqual(str(request.url), SETTINGS.base_url + "/v1/chat/completions")
                        self.assertEqual(request.method, "POST")
                        self.assertEqual(request.headers["content-type"], "application/json")
                        self.assertEqual(request.extensions["timeout"]["read"], 45)
                        body = json.loads(request.content)
                        self.assertEqual(body["model"], settings.model)
                        self.assertIs(body["stream"], False)
                        self.assertEqual(len(body["messages"]), 1)
                        message = body["messages"][0]
                        self.assertEqual(message["role"], "user")
                        parts = message["content"]
                        self.assertEqual([part["type"] for part in parts], ["image_url", "text"])
                        prefix, data = parts[0]["image_url"]["url"].split(",", 1)
                        self.assertEqual(prefix, f"data:{mime};base64")
                        self.assertEqual(base64.b64decode(data), image_bytes)
                        self.assertEqual(parts[1]["text"], settings.prompt)
                        return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})

                    with mock_client(handler):
                        client = create_ocr_client(settings)
                        try:
                            self.assertIsInstance(client, OpenAILikeOCRClient)
                            self.assertEqual(client.recognize(image_bytes), raw)
                        finally:
                            client.close()
                        self.assertTrue(client.client.is_closed)

    def test_empty_text_is_valid(self):
        with mock_client(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})):
            client = create_ocr_client(SETTINGS)
            try:
                self.assertEqual(client.recognize(encoded_image()), "")
            finally:
                client.close()

    def test_upstream_failures_and_malformed_responses_are_translated(self):
        def offline(request):
            raise httpx.ConnectError("offline", request=request)

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        handlers = [(offline, OCRBackendError), (timeout, OCRTimeoutError),
                    (lambda request: httpx.Response(404, json={"error": "model not found"}), OCRBackendError),
                    (lambda request: httpx.Response(500), OCRBackendError),
                    (lambda request: httpx.Response(200, text="bad json"), OCRBackendError)]
        malformed = (None, [], {}, {"choices": []}, {"choices": None}, {"choices": [None]},
                     {"choices": [{}]}, {"choices": [{"message": None}]},
                     {"choices": [{"message": {}}]}, {"choices": [{"message": {"content": None}}]},
                     {"choices": [{"message": {"content": []}}]})
        handlers.extend((lambda request, body=body: httpx.Response(200, content=json.dumps(body)), OCRBackendError)
                        for body in malformed)
        for handler, error in handlers:
            with self.subTest(handler=handler), mock_client(handler):
                client = create_ocr_client(SETTINGS)
                try:
                    with self.assertRaises(error):
                        client.recognize(encoded_image())
                finally:
                    client.close()

    def test_factory_service_cleanup_and_health_do_not_contact_server(self):
        def unexpected(request):
            self.fail("Construction, invalid input and health must not contact the backend")

        select_provider(self, SETTINGS)
        with mock_client(unexpected):
            with self.assertRaises(OCRInputError):
                with create_service(SETTINGS) as service:
                    transport = service.client.client
                    service.ocr(BytesIO(b"invalid"), "input.png")
            self.assertTrue(transport.is_closed)
            with TestClient(app) as api:
                self.assertEqual(api.get("/api/health").status_code, 200)
        with patch("app.ollama_client.Client") as ollama:
            client = create_ocr_client(replace(SETTINGS, provider="ollama"))
            self.assertIsInstance(client, OllamaOCRClient)
            client.close()
            ollama.return_value.close.assert_called_once()
        with self.assertRaises(ConfigError):
            create_ocr_client(replace(SETTINGS, provider="unknown"))


class OpenAILikeAdapterTests(unittest.TestCase):
    def setUp(self):
        select_provider(self, SETTINGS)

    def test_cli_and_api_normalize_images_and_export_raw_and_grounded_crops(self):
        raw = "  café\nimage[[0,0,999,999]]\n"
        calls = []

        def handler(request):
            body = json.loads(request.content)
            prefix, data = body["messages"][0]["content"][0]["image_url"]["url"].split(",", 1)
            self.assertEqual(prefix, "data:image/png;base64")
            with Image.open(BytesIO(base64.b64decode(data))) as image:
                self.assertEqual((image.format, image.mode), ("PNG", "RGB"))
            calls.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})

        original = encoded_image("BMP")
        with mock_client(handler), tempfile.TemporaryDirectory() as root:
            source = Path(root, "image.bmp")
            source.write_bytes(original)
            stdout = StringIO()
            with redirect_stdout(stdout), redirect_stderr(StringIO()):
                self.assertEqual(main(["ocr", str(source), "--output-path", root, "--save-raw"]), 0)
            exported = Path(stdout.getvalue().strip())
            self.assertEqual((exported / "raw.txt").read_bytes(), raw.encode())
            self.assertTrue((exported / "image-001.png").exists())
            with TestClient(app) as api:
                response = api.post("/api/ocr", files={"file": ("image.bmp", original)})
            self.assertEqual(response.status_code, 200, response.text)
            with ZipFile(BytesIO(response.content)) as archive:
                self.assertEqual({Path(name).name: archive.read(name) for name in archive.namelist()},
                                 {name: (exported / name).read_bytes() for name in ("document.md", "image-001.png")})
        self.assertEqual(len(calls), 2)

    def test_multipage_pdf_cli_and_streaming_api(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "recognized\n"}}]})

        original = pdf_bytes(2)
        with mock_client(handler), tempfile.TemporaryDirectory() as root:
            source = Path(root, "book.pdf")
            source.write_bytes(original)
            stdout = StringIO()
            with redirect_stdout(stdout), redirect_stderr(StringIO()):
                self.assertEqual(main(["ocr", str(source), "--output-path", root, "--pdf-dpi", "72",
                                       "--save-raw", "--llm-workers", "2"]), 0)
            exported = Path(stdout.getvalue().strip())
            self.assertEqual((exported / "page-0001-raw.txt").read_bytes(), b"recognized\n")
            self.assertEqual((exported / "page-0002-raw.txt").read_bytes(), b"recognized\n")
            markdown = (exported / "document.md").read_bytes()
            self.assertLess(markdown.index(b"<!-- Page 1 -->"), markdown.index(b"<!-- Page 2 -->"))
            with TestClient(app) as api:
                response = api.post("/api/ocr/stream", files={"file": ("book.pdf", original)},
                                    data={"pdf_dpi": "72"})
                self.assertEqual(response.status_code, 200)
                events = parse_events(response.text)
                self.assertIn("event: job.completed\n", response.text)
                job_id = events[0]["job_id"]
                download = api.get(f"/api/ocr/jobs/{job_id}/download")
                self.assertEqual(download.status_code, 200)
                with ZipFile(BytesIO(download.content)) as archive:
                    self.assertEqual(archive.read(archive.namelist()[0]), markdown)
        self.assertEqual(len(calls), 4)

    def test_http_errors_and_timeouts_reach_cli_and_api(self):
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        for handler, status in ((lambda request: httpx.Response(503), 502), (timeout, 504)):
            with self.subTest(status=status), mock_client(handler), tempfile.TemporaryDirectory() as root:
                source = Path(root, "input.png")
                source.write_bytes(encoded_image())
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(["ocr", str(source), "--output-path", root]), 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("OpenAI like", stderr.getvalue())
                with TestClient(app) as api:
                    response = api.post("/api/ocr", files={"file": ("input.png", source.read_bytes())})
                    self.assertEqual(response.status_code, status)


if __name__ == "__main__":
    unittest.main()
