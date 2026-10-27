"""Behavior checks for config, shared service, CLI, and HTTP adapters."""

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.__main__ import main
from app.api import app
from app.config import ConfigError, OllamaSettings, load_settings
from app.errors import OCRBackendError, OCRInputError, OCROutputError, OCRTimeoutError
from app.ollama_client import OllamaOCRClient
from app.service import OCRService, create_service
from test_processing import encoded_image


class ConfigTests(unittest.TestCase):
    def test_default_config_is_independent_of_working_directory(self):
        expected = load_settings()
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.assertEqual(load_settings(), expected)
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
    def test_builds_document_after_image_preparation(self):
        client = Mock()
        raw = '  # OCR\n"quoted" café\n\n'
        client.recognize.return_value = raw
        file = BytesIO(b"original image")
        prepared = encoded_image()
        with patch("app.service.prepare_image", return_value=prepared) as prepare:
            result = OCRService(client).ocr(file, "input.png")
        self.assertEqual((result.width, result.height), (3, 2))
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].content, raw)
        prepare.assert_called_once_with(file, "input.png")
        client.recognize.assert_called_once_with(prepared)

    def test_configures_client_without_inference_and_closes_after_failure(self):
        settings = OllamaSettings("http://example.invalid:11434", "deepseek-ocr", 45)
        with patch("app.ollama_client.Client") as client:
            with self.assertRaises(OCRInputError):
                with create_service(settings) as service:
                    service.ocr(BytesIO(b"input"), "input.png")
            client.assert_called_once_with(host=settings.base_url, timeout=45)
            client.return_value.chat.assert_not_called()
            client.return_value.generate.assert_not_called()
            client.return_value.close.assert_called_once()



class AdapterTests(unittest.TestCase):
    def test_cli_and_api_export_identical_plain_markdown(self):
        with tempfile.TemporaryDirectory() as directory, TestClient(app) as client:
            path = Path(directory) / "input.png"
            path.write_bytes(encoded_image())
            for raw in ('  # OCR\n"quoted" café\r\n\n', "no trailing newline", ""):
                with self.subTest(raw=raw):
                    stdout, stderr = StringIO(), StringIO()
                    with patch.object(OllamaOCRClient, "recognize", return_value=raw):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            status = main(["ocr", str(path), "--output-path", directory])
                        response = client.post(
                            "/api/ocr",
                            files={"file": ("input.png", encoded_image(), "image/png")},
                        )
                    self.assertEqual(status, 0)
                    self.assertIn("archive completed", stderr.getvalue())
                    exported = Path(stdout.getvalue().strip())
                    self.assertEqual(exported.parent, Path(directory))
                    self.assertEqual((exported / "document.md").read_bytes(), raw.encode("utf-8"))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["content-type"], "application/zip")
                    self.assertIn("attachment; filename=", response.headers["content-disposition"])
                    with ZipFile(BytesIO(response.content)) as archive:
                        self.assertEqual(len(archive.namelist()), 1)
                        self.assertEqual(Path(archive.namelist()[0]).name, "document.md")
                        self.assertEqual(archive.read(archive.namelist()[0]), raw.encode("utf-8"))

    def test_cli_missing_file_and_invalid_image_have_no_success_output(self):
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
                self.assertIn("valid supported image" if exists else "No such file", stderr.getvalue())

    def test_cli_invalid_config_is_a_concise_error(self):
        stderr = StringIO()
        with patch("app.__main__.load_settings", side_effect=ConfigError("Invalid settings")):
            with redirect_stderr(stderr):
                self.assertEqual(main(["serve"]), 1)
        self.assertEqual(stderr.getvalue(), "Error: Invalid settings\n")

    def test_health_invalid_upload_and_required_file(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            response = client.post("/api/ocr", files={"file": ("input.png", b"input", "image/png")})
            self.assertEqual(response.status_code, 400)
            self.assertIn("valid supported image", response.json()["detail"])
            self.assertEqual(client.post("/api/ocr").status_code, 422)

    def test_backend_errors_have_no_success_output(self):
        with tempfile.TemporaryDirectory() as directory, TestClient(app) as client:
            path = Path(directory) / "input.png"
            path.write_bytes(encoded_image())
            for error, status_code in (
                (OCRBackendError("Ollama unavailable"), 502),
                (OCRTimeoutError("Ollama timed out"), 504),
                (OCROutputError("Malformed grounding"), 502),
            ):
                with self.subTest(error=error):
                    stdout, stderr = StringIO(), StringIO()
                    with patch.object(OllamaOCRClient, "recognize", side_effect=error):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            status = main(["ocr", str(path)])
                        response = client.post(
                            "/api/ocr", files={"file": ("input.png", encoded_image(), "image/png")}
                        )
                    self.assertEqual(status, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(str(error), stderr.getvalue())
                    self.assertEqual(response.status_code, status_code)
                    self.assertEqual(response.json()["detail"], str(error))

    def test_invalid_config_prevents_api_startup(self):
        with patch("app.api.load_settings", side_effect=ConfigError("Invalid settings")):
            with self.assertRaisesRegex(ConfigError, "Invalid settings"):
                with TestClient(app):
                    pass


if __name__ == "__main__":
    unittest.main()
