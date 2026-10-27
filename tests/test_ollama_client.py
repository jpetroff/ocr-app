"""Exercise the actual Ollama SDK over a controlled HTTP transport."""

import base64
from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient
from ollama import Client

from app.__main__ import main
from app.api import app
from app.config import OllamaSettings
from app.errors import OCRBackendError, OCRTimeoutError
from app.ollama_client import OllamaOCRClient
from test_processing import encoded_image


SETTINGS = OllamaSettings("http://ollama.test:11434", "deepseek-ocr:3b", 45)


def sdk_client(handler):
    return patch(
        "app.ollama_client.Client",
        side_effect=lambda **kwargs: Client(transport=httpx.MockTransport(handler), **kwargs),
    )


class OllamaClientTests(unittest.TestCase):
    def test_sdk_sends_image_and_preserves_raw_text(self):
        image_bytes = encoded_image()
        raw = '  # text\n"café"\n\n'

        def handler(request):
            self.assertEqual(str(request.url), SETTINGS.base_url + "/api/generate")
            self.assertEqual(request.method, "POST")
            body = json.loads(request.content)
            self.assertEqual(body["model"], SETTINGS.model)
            self.assertEqual(body["prompt"], "<|grounding|>Convert the document to markdown.")
            self.assertIs(body["stream"], False)
            self.assertEqual(base64.b64decode(body["images"][0]), image_bytes)
            self.assertEqual(len(body["images"]), 1)
            self.assertEqual(request.extensions["timeout"]["read"], 45)
            return httpx.Response(200, json={"response": raw, "done": True})

        with sdk_client(handler):
            client = OllamaOCRClient(SETTINGS)
            try:
                self.assertEqual(client.recognize(image_bytes), raw)
            finally:
                client.close()

    def test_empty_text_is_a_valid_response(self):
        with sdk_client(lambda request: httpx.Response(200, json={"response": ""})):
            client = OllamaOCRClient(SETTINGS)
            try:
                self.assertEqual(client.recognize(encoded_image()), "")
            finally:
                client.close()

    def test_upstream_failures_are_translated(self):
        def connection_failure(request):
            raise httpx.ConnectError("offline", request=request)

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        cases = (
            (connection_failure, OCRBackendError),
            (timeout, OCRTimeoutError),
            (lambda request: httpx.Response(404, json={"error": "model not found"}), OCRBackendError),
            (lambda request: httpx.Response(500, json={"error": "inference failed"}), OCRBackendError),
            (lambda request: httpx.Response(200, text="invalid json"), OCRBackendError),
            (lambda request: httpx.Response(200, json={"done": True}), OCRBackendError),
        )
        for handler, expected in cases:
            with self.subTest(handler=handler), sdk_client(handler):
                client = OllamaOCRClient(SETTINGS)
                try:
                    with self.assertRaises(expected):
                        client.recognize(encoded_image())
                finally:
                    client.close()

    def test_cli_and_api_run_full_flow_with_conversion_and_sdk(self):
        raw = "  Raw OCR café\n"
        original = encoded_image("BMP")

        def handler(request):
            sent = base64.b64decode(json.loads(request.content)["images"][0])
            from PIL import Image
            with Image.open(BytesIO(sent)) as image:
                self.assertEqual((image.format, image.mode), ("PNG", "RGB"))
            return httpx.Response(200, json={"response": raw})

        with sdk_client(handler), tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.bmp"
            path.write_bytes(original)
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["ocr", str(path), "--output-path", directory]), 0)
            with TestClient(app) as client:
                response = client.post("/api/ocr", files={"file": ("input.bmp", original)})
            exported = Path(stdout.getvalue().strip())
            self.assertEqual((exported / "document.md").read_bytes(), raw.encode())
            self.assertEqual(response.status_code, 200)
            with ZipFile(BytesIO(response.content)) as archive:
                self.assertEqual(archive.read(archive.namelist()[0]), raw.encode())


if __name__ == "__main__":
    unittest.main()
