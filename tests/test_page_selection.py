"""Range syntax, selected task scheduling and CLI/HTTP page-selection contracts."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.__main__ import main
from app.api import app
from app.errors import OCRBackendError, OCRInputError
from app.ollama_client import OllamaOCRClient
from app.page_selection import PageSelection, parse_page_ranges
from test_pipeline import ControlledPipeline, FakeClient, parse_events, pdf_bytes
from test_processing import encoded_image


def selection(value, *, exclude=False):
    return PageSelection(**{"exclude" if exclude else "include": parse_page_ranges(value)})


def zip_markdown(content):
    with ZipFile(BytesIO(content)) as archive:
        name = next(n for n in archive.namelist() if n.endswith("document.md"))
        return archive.read(name).decode(), archive.namelist()


class RangeTests(unittest.TestCase):
    def test_all_range_types_are_inclusive_deduplicated_and_source_ordered(self):
        self.assertEqual(selection("1-23,45-44,50,79-").resolve(81),
                         list(range(1, 24)) + [44, 45, 50, 79, 80, 81])
        self.assertEqual(selection(" -3, 5-4, 3, 2-2 ").resolve(6), [1, 2, 3, 4, 5])
        self.assertEqual(selection("6-,6").resolve(6), [6])
        self.assertEqual(selection("-").resolve(3), [1, 2, 3])
        self.assertEqual(PageSelection().resolve(3), [1, 2, 3])
        self.assertEqual(selection("-2,4-3,6-", exclude=True).resolve(7), [5])

    def test_malformed_ranges_and_zero_pages_are_rejected(self):
        for value in ("", " ", "1,", ",1", "1,,2", "0", "0-2", "2-0", "0-",
                      "-0", "1--2", "1-2-3", "1.5", "+2", "one", "1 - 3"):
            with self.subTest(value=value), self.assertRaises(OCRInputError):
                parse_page_ranges(value)

    def test_bounds_empty_selection_and_mutual_exclusivity(self):
        for value in ("4", "4-", "-4", "1-1000000000000"):
            for exclude in (False, True):
                with self.subTest(value=value, exclude=exclude), self.assertRaisesRegex(OCRInputError, "out of range"):
                    selection(value, exclude=exclude).resolve(3)
        with self.assertRaisesRegex(OCRInputError, "no pages"):
            selection("-", exclude=True).resolve(3)
        with self.assertRaisesRegex(OCRInputError, "empty"):
            PageSelection(include=())
        with self.assertRaisesRegex(OCRInputError, "mutually exclusive"):
            PageSelection(include=parse_page_ranges("1"), exclude=parse_page_ranges("2"))


class SelectedSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_selected_pages_reach_workers_and_preserve_source_numbers(self):
        for exclude in (False, True):
            client = FakeClient()
            pipeline = ControlledPipeline(count=6, client_factory=lambda: client)
            self.addAsyncCleanup(pipeline.close)
            chosen = selection("6,2-3,2" if not exclude else "1,4-5", exclude=exclude)
            job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf", save_raw=True, page_selection=chosen)
            await asyncio.wait_for(job.done.wait(), 10)
            self.assertEqual(job.status, "completed")
            self.assertEqual(sorted(page for _, page in client.calls), [2, 3, 6])
            self.assertEqual(job.plan.page_count, 3)
            self.assertEqual(job.stats()["pages_source"], 6)
            self.assertEqual(job.stats()["pages_completed"], 3)
            self.assertEqual({t.page for t in job.plan.tasks.values() if t.page}, {2, 3, 6})
            self.assertEqual(job.plan.tasks["assemble"].dependencies,
                             [f"page-{p}:postprocess" for p in (2, 3, 6)])
            planned = next(data for event, data in job.events if event == "job.planned")
            self.assertEqual(planned["selected_pages"], [2, 3, 6])
            text, names = zip_markdown(Path(job.result.archive).read_bytes())
            self.assertEqual([line for line in text.splitlines() if line.startswith("<!-- Page")],
                             [f"<!-- Page {p} -->" for p in (2, 3, 6)])
            self.assertTrue(any("page-0006-image-001.png" in name for name in names))
            self.assertEqual(sorted(p.name for p in job.root.glob("raw-*.txt")),
                             ["raw-2.txt", "raw-3.txt", "raw-6.txt"])

    async def test_invalid_selection_runs_no_page_workers_and_has_no_archive(self):
        client = FakeClient()
        pipeline = ControlledPipeline(count=3, client_factory=lambda: client)
        self.addAsyncCleanup(pipeline.close)
        for chosen in (selection("4"), selection("-", exclude=True)):
            job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf", page_selection=chosen)
            await asyncio.wait_for(job.done.wait(), 10)
            self.assertEqual(job.status, "failed")
            self.assertIsInstance(job.error, OCRInputError)
            self.assertIsNone(job.result.archive)
            self.assertFalse(any(t.page for t in job.plan.tasks.values()))
        self.assertEqual(client.calls, [])

    async def test_one_selected_page_failure_retains_original_exception(self):
        client = FakeClient(lambda page: (_ for _ in ()).throw(OCRBackendError("offline")))
        pipeline = ControlledPipeline(count=6, client_factory=lambda: client)
        self.addAsyncCleanup(pipeline.close)
        job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf", page_selection=selection("4"))
        await asyncio.wait_for(job.done.wait(), 10)
        self.assertEqual(job.status, "failed")
        self.assertIsInstance(job.error, OCRBackendError)
        self.assertEqual(job.result.failed_pages[0]["page"], 4)


class SelectionAdapterTests(unittest.TestCase):
    def setUp(self):
        from provider_helpers import select_provider
        select_provider(self)

    def test_cli_ranges_include_exclude_and_raw_exports(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "book.pdf")
            source.write_bytes(pdf_bytes(5))
            for options, pages in ((["--include=-2,5-4,2"], [1, 2, 4, 5]),
                                   (["--exclude=3-,1"], [2])):
                stdout, stderr = StringIO(), StringIO()
                with patch.object(OllamaOCRClient, "recognize", return_value="recognized") as recognize:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        status = main(["ocr", str(source), "--output", root, "--pdf-dpi", "72",
                                       "--save-raw", *options])
                self.assertEqual(status, 0, stderr.getvalue())
                self.assertEqual(recognize.call_count, len(pages))
                output = Path(stdout.getvalue().strip())
                text = (output / "document.md").read_text()
                self.assertEqual([line for line in text.splitlines() if line.startswith("<!-- Page")],
                                 [f"<!-- Page {p} -->" for p in pages])
                self.assertEqual(sorted(p.name for p in output.glob("*-raw.txt")),
                                 [f"page-{p:04d}-raw.txt" for p in pages])

    def test_cli_rejects_conflicting_and_invalid_arguments_before_processing(self):
        for options in (["--include", "1", "--exclude", "2"], ["--include", "1,,2"],
                        ["--exclude", "0"]):
            with redirect_stderr(StringIO()), patch("app.__main__.load_settings") as load:
                with self.assertRaises(SystemExit) as raised:
                    main(["ocr", "unused.pdf", *options])
            self.assertEqual(raised.exception.code, 2)
            load.assert_not_called()

    def test_both_api_endpoints_accept_repeated_integer_fields(self):
        with TestClient(app) as client:
            for endpoint in ("/api/ocr", "/api/ocr/stream"):
                for data in ({"include": ["3", "1", "3"]}, {"exclude": ["2"]}):
                    with patch.object(OllamaOCRClient, "recognize", return_value="recognized") as recognize:
                        response = client.post(endpoint, files={"file": ("book.pdf", pdf_bytes())},
                                               data={"pdf_dpi": "72", **data})
                    self.assertEqual(response.status_code, 200, response.text[:100] if endpoint.endswith("stream") else "")
                    self.assertEqual(recognize.call_count, 2)
                    if endpoint.endswith("stream"):
                        events = parse_events(response.text)
                        planned = next(e for e in events if "selected_pages" in e)
                        self.assertEqual(planned["selected_pages"], [1, 3])
                        snapshot = client.get(f"/api/ocr/jobs/{events[0]['job_id']}").json()
                        self.assertEqual(snapshot["stats"]["pages_total"], 2)
                        self.assertEqual(snapshot["stats"]["pages_source"], 3)
                        self.assertEqual(snapshot["stats"]["pages_completed"], 2)
                        response = client.get(snapshot["result"]["download_url"])
                    text, _ = zip_markdown(response.content)
                    self.assertIn("<!-- Page 1 -->", text)
                    self.assertIn("<!-- Page 3 -->", text)
                    self.assertNotIn("<!-- Page 2 -->", text)

    def test_api_rejects_malformed_fields_and_conflicts_before_acceptance(self):
        with TestClient(app) as client, patch.object(OllamaOCRClient, "recognize") as recognize:
            for endpoint in ("/api/ocr", "/api/ocr/stream"):
                for data in ({"include": "0"}, {"exclude": "-1"}, {"include": "1-3"},
                             {"exclude": "1,3"}, {"include": "1.5"}, {"include": ""},
                             {"include": ["1", "0"]}, {"include": "1", "exclude": "2"}):
                    with self.subTest(endpoint=endpoint, data=data):
                        response = client.post(endpoint, files={"file": ("book.pdf", pdf_bytes())}, data=data)
                        self.assertEqual(response.status_code, 422, response.text)
            recognize.assert_not_called()
            self.assertEqual(client.app.state.ocr_service.jobs, {})

    def test_document_dependent_errors_for_blocking_streaming_and_raster(self):
        with TestClient(app) as client, patch.object(OllamaOCRClient, "recognize") as recognize:
            for filename, content, data in (("book.pdf", pdf_bytes(), {"include": "4"}),
                                             ("book.pdf", pdf_bytes(), {"exclude": ["1", "2", "3"]}),
                                             ("image.png", encoded_image(), {"include": "2"}),
                                             ("image.png", encoded_image(), {"exclude": "1"})):
                for endpoint in ("/api/ocr", "/api/ocr/stream"):
                    response = client.post(endpoint, files={"file": (filename, content)}, data=data)
                    if endpoint.endswith("stream"):
                        self.assertEqual(response.status_code, 200)
                        terminal = parse_events(response.text)[-1]
                        self.assertEqual(terminal["result"]["status"], "failed")
                        self.assertIsNone(terminal["result"]["download_url"])
                    else:
                        self.assertEqual(response.status_code, 400, response.text)
            recognize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
