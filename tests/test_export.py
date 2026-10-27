"""Directory/ZIP contract and temporary-response ownership checks."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.__main__ import main
from app.api import app, TemporaryZipResponse
from app.document import build_document
from app.export import archive_document, export_document
from app.ollama_client import OllamaOCRClient
from test_document import color_image, FIXTURE


def zipped_files(data):
    with ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        roots = {name.split("/")[0] for name in names}
        if len(roots) != 1 or any(len(name.split("/")) != 2 for name in names):
            raise AssertionError("Archive must contain one top-level document directory")
        return {Path(name).name: archive.read(name) for name in names}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.image = color_image((1000, 1200))
        self.text = FIXTURE.read_text()
        self.document = build_document(self.text, self.image)

    def test_unique_safe_directories_and_relative_image_references(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "new" / "output"
            first = export_document(self.document, parent, "../../A book.png")
            second = export_document(self.document, parent, r"C:\uploads\A book.png")
            fallback = export_document(self.document, parent, "../../....png")
            self.assertEqual(first.parent, parent)
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("A-book-"))
            self.assertTrue(second.name.startswith("A-book-"))
            self.assertTrue(fallback.name.startswith("document-"))
            self.assertEqual({path.name for path in first.iterdir()}, {"document.md", "image-001.png"})
            markdown = (first / "document.md").read_text()
            self.assertIn("![Image](image-001.png)", markdown)
            self.assertNotIn(str(first), markdown)
            self.assertEqual((first / "image-001.png").read_bytes(), self.document.blocks[2].images[0].data)

    def test_disk_zip_contains_identical_files_under_one_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported = export_document(self.document, root, "input.png")
            archive = root / "document.zip"
            archive_document(exported, archive)
            with ZipFile(archive) as output:
                self.assertEqual({name.split("/")[0] for name in output.namelist()}, {exported.name})
                self.assertEqual(
                    {Path(name).name: output.read(name) for name in output.namelist()},
                    {file.name: file.read_bytes() for file in exported.iterdir()},
                )

    def test_failed_write_removes_only_incomplete_directory(self):
        for writer in ("write_text", "write_bytes"):
            with self.subTest(writer=writer), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                existing = export_document(self.document, parent, "existing.png")
                with patch.object(Path, writer, side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        export_document(self.document, parent, "failed.png")
                self.assertEqual(list(parent.iterdir()), [existing])
                self.assertTrue((existing / "document.md").exists())

    def test_cli_defaults_to_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.png"
            source.write_bytes(self.image)
            previous = Path.cwd()
            try:
                os.chdir(directory)
                stdout = StringIO()
                with patch.object(OllamaOCRClient, "recognize", return_value=self.text), redirect_stdout(stdout):
                    self.assertEqual(main(["ocr", str(source)]), 0)
                exported = Path(stdout.getvalue().strip())
                self.assertTrue(exported.is_absolute())
                self.assertEqual(exported.parent, Path(directory))
                self.assertTrue((exported / "image-001.png").exists())
            finally:
                os.chdir(previous)

    def test_cli_and_api_fixture_exports_match_and_api_workspace_is_removed(self):
        created = []
        temporary_directory = tempfile.TemporaryDirectory

        def workspace(**kwargs):
            self.assertEqual(kwargs["dir"], "/tmp")
            result = temporary_directory(**kwargs)
            created.append(Path(result.name))
            return result

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.png"
            source.write_bytes(self.image)
            stdout = StringIO()
            with patch.object(OllamaOCRClient, "recognize", return_value=self.text):
                with redirect_stdout(stdout):
                    status = main(["ocr", str(source), "--output-path", str(Path(directory) / "out")])
                with TestClient(app) as client, patch("app.api.TemporaryDirectory", side_effect=workspace):
                    response = client.post("/api/ocr", files={"file": ("input.png", self.image)})
            self.assertEqual(status, 0)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/zip")
            exported = Path(stdout.getvalue().strip())
            self.assertEqual(zipped_files(response.content), {file.name: file.read_bytes() for file in exported.iterdir()})
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists())

    def test_api_export_failures_remove_workspace_and_return_500(self):
        temporary_directory = tempfile.TemporaryDirectory
        created = []

        def workspace(**kwargs):
            result = temporary_directory(**kwargs)
            created.append(Path(result.name))
            return result

        for entry in ("export_document", "archive_document", "TemporaryZipResponse"):
            with self.subTest(entry=entry), TestClient(app) as client:
                with patch.object(OllamaOCRClient, "recognize", return_value=self.text):
                    with patch("app.api.TemporaryDirectory", side_effect=workspace):
                        with patch("app.api." + entry, side_effect=OSError("disk full")):
                            response = client.post("/api/ocr", files={"file": ("input.png", self.image)})
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"detail": "Could not export the OCR document."})
                self.assertFalse(created[-1].exists())

    def test_malformed_output_is_502_without_export(self):
        with TestClient(app) as client, patch.object(OllamaOCRClient, "recognize", return_value="image[[bad]]"):
            with patch("app.api.TemporaryDirectory") as workspace:
                response = client.post("/api/ocr", files={"file": ("input.png", self.image)})
                self.assertEqual(response.status_code, 502)
                workspace.assert_not_called()

    def test_cli_filesystem_failure_has_no_success_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.png"
            source.write_bytes(self.image)
            stdout, stderr = StringIO(), StringIO()
            with patch.object(OllamaOCRClient, "recognize", return_value=self.text):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    # An existing file cannot be an output directory.
                    status = main(["ocr", str(source), "--output-path", str(source)])
            self.assertEqual(status, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue().startswith("Error: "))

    def test_openapi_describes_zip_success(self):
        content = app.openapi()["paths"]["/api/ocr"]["post"]["responses"]["200"]["content"]
        self.assertEqual(set(content), {"application/zip"})


class TemporaryResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="ocr-test-", dir="/tmp")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        archive = self.root / "document.zip"
        # More than one FileResponse chunk, without relying on ZIP compressibility.
        self.data = b"x" * (TemporaryZipResponse.chunk_size + 11)
        archive.write_bytes(self.data)
        self.response = TemporaryZipResponse(archive, self.workspace)
        self.scope = {
            "type": "http", "method": "POST", "headers": [],
            "asgi": {"spec_version": "2.4"},
            "extensions": {"http.response.pathsend": {}},
        }

    async def receive(self):
        raise AssertionError("ASGI 2.4 file response should send without receiving")

    async def test_files_live_through_last_chunk_then_are_removed(self):
        chunks = []

        async def send(message):
            self.assertTrue(self.root.exists())
            self.assertNotEqual(message["type"], "http.response.pathsend")
            if message["type"] == "http.response.body":
                chunks.append(message["body"])

        await self.response(self.scope, self.receive, send)
        self.assertEqual(b"".join(chunks), self.data)
        self.assertFalse(self.root.exists())

    async def test_failed_transmission_removes_files(self):
        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with self.assertRaises(OSError):
            await self.response(self.scope, self.receive, send)
        self.assertFalse(self.root.exists())

    async def test_cancelled_transmission_removes_files(self):
        body_started = asyncio.Event()
        hold = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                body_started.set()
                await hold.wait()

        task = asyncio.create_task(self.response(self.scope, self.receive, send))
        try:
            await asyncio.wait_for(body_started.wait(), timeout=3)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.root.exists())

    async def test_legacy_disconnect_removes_files(self):
        body_started = asyncio.Event()
        hold = asyncio.Event()
        scope = dict(self.scope, asgi={"spec_version": "2.0"})

        async def receive():
            await body_started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                body_started.set()
                await hold.wait()

        await asyncio.wait_for(self.response(scope, receive, send), timeout=3)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
