"""Behavior checks for config, shared service, CLI, and HTTP adapters."""

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.__main__ import main
from app.api import app
from app.config import ConfigError, OllamaSettings, load_settings
from app.errors import OCRNotImplementedError
from app.ollama_client import OllamaOCRClient
from app.processing import ImagePage
from app.service import OCRPage, OCRResult, OCRService, create_service


class ConfigTests(unittest.TestCase):
    def test_default_config_is_independent_of_working_directory(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.assertEqual(load_settings().model, "deepseek-ocr")
            finally:
                os.chdir(previous)

    def test_invalid_config_fails_clearly(self):
        invalid_documents = [
            "", "ollama: [", "ollama: []",
            "ollama: {base_url: ftp://localhost, model: deepseek-ocr, timeout_seconds: 120}",
            "ollama: {base_url: 'http://localhost:bad', model: deepseek-ocr, timeout_seconds: 120}",
            "ollama: {base_url: http://localhost, model: '', timeout_seconds: 120}",
            "ollama: {base_url: http://localhost, model: deepseek-ocr, timeout_seconds: true}",
            "ollama: {base_url: http://localhost, model: deepseek-ocr, timeout_seconds: -1}",
            "ollama: {base_url: http://localhost, model: deepseek-ocr, timeout_seconds: .inf}",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_settings(path)
            with self.assertRaises(ConfigError):
                load_settings(Path(directory) / "missing.yaml")


class ServiceTests(unittest.TestCase):
    def test_preserves_page_order_and_calls_inference_per_page(self):
        client = Mock()
        client.recognize.side_effect = ["first", "second"]
        file = BytesIO(b"document")
        pages = [ImagePage(1, b"png1"), ImagePage(2, b"png2")]
        with patch("app.service.iter_pages", return_value=iter(pages)) as convert:
            result = OCRService(client).ocr(file, "document.pdf")
        self.assertEqual(result, OCRResult([OCRPage(1, "first"), OCRPage(2, "second")]))
        convert.assert_called_once_with(file, "document.pdf")
        self.assertEqual([call.args for call in client.recognize.call_args_list], [(b"png1",), (b"png2",)])

    def test_configures_client_without_inference_and_closes_after_failure(self):
        settings = OllamaSettings("http://example.invalid:11434", "deepseek-ocr", 45)
        with patch("app.ollama_client.Client") as client:
            with self.assertRaises(OCRNotImplementedError):
                with create_service(settings) as service:
                    service.ocr(BytesIO(b"input"), "input.png")
            client.assert_called_once_with(host=settings.base_url, timeout=45)
            client.return_value.chat.assert_not_called()
            client.return_value.generate.assert_not_called()
            client.return_value.close.assert_called_once()

    def test_inference_is_also_an_explicit_stub(self):
        with patch("app.ollama_client.Client"):
            client = OllamaOCRClient(load_settings())
            try:
                with self.assertRaisesRegex(OCRNotImplementedError, "inference"):
                    client.recognize(b"png")
            finally:
                client.close()


class AdapterTests(unittest.TestCase):
    def test_cli_missing_file_and_stub_have_no_success_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.png"
            for exists in (False, True):
                if exists:
                    path.write_bytes(b"input")
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main(["ocr", str(path)])
                self.assertEqual(status, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("not implemented" if exists else "No such file", stderr.getvalue())

    def test_cli_invalid_config_is_a_concise_error(self):
        stderr = StringIO()
        with patch("app.__main__.load_settings", side_effect=ConfigError("Invalid settings")):
            with redirect_stderr(stderr):
                self.assertEqual(main(["serve"]), 1)
        self.assertEqual(stderr.getvalue(), "Error: Invalid settings\n")

    def test_health_upload_stub_and_required_file(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            response = client.post("/api/ocr", files={"file": ("input.png", b"input", "image/png")})
            self.assertEqual(response.status_code, 501)
            self.assertIn("not implemented", response.json()["detail"])
            self.assertEqual(client.post("/api/ocr").status_code, 422)

    def test_invalid_config_prevents_api_startup(self):
        with patch("app.api.load_settings", side_effect=ConfigError("Invalid settings")):
            with self.assertRaisesRegex(ConfigError, "Invalid settings"):
                with TestClient(app):
                    pass


if __name__ == "__main__":
    unittest.main()
