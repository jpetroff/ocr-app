"""Real PDF rendering plus deterministic scheduler, event and lifetime checks."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from contextlib import closing
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image
import pypdfium2 as pdfium

from app.__main__ import main
from app.api import app, JobZipResponse, stream_ocr
from app.config import ConfigError, OllamaSettings, PipelineSettings, load_settings
from app.errors import OCRBackendError, OCRInputError
from app.ollama_client import OllamaOCRClient
from app.pipeline import (Job, Pipeline, inspect_input, postprocess_page, prepare_page)
from test_processing import encoded_image


def pdf_bytes(count=3):
    output = BytesIO()
    with pdfium.PdfDocument.new() as pdf:
        for page in range(count):
            with closing(pdf.new_page(72 + page * 10, 144)) as item:
                if page == 1:
                    item.set_rotation(90)
        pdf.save(output)
    return output.getvalue()


def parse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


class PDFWorkerTests(unittest.TestCase):
    def test_page_dimensions_dpi_rotation_and_blank_white_background(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "input")
            source.write_bytes(pdf_bytes())
            self.assertEqual(inspect_input(str(source), root, "misleading.png"), (True, 3))
            for page, dpi, expected in ((1, 72, (72, 144)), (1, 144, (144, 288)),
                                        (2, 72, (144, 82)), (3, 72, (92, 144)),
                                        (1, 300, (300, 600))):
                path = prepare_page(str(source), root, page, dpi, True)
                with Image.open(path) as image:
                    self.assertEqual((image.mode, image.size), ("RGB", expected))
                    self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_content_detection_and_invalid_pdf(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "input")
            source.write_bytes(encoded_image())
            self.assertEqual(inspect_input(str(source), root, "misleading.pdf"), (False, 1))
            self.assertEqual(Path(root, "prepared-1.png").read_bytes(), encoded_image())
            source.write_bytes(b"%PDF-1.7\nbroken")
            with self.assertRaises(OCRInputError):
                inspect_input(str(source), root, "input.pdf")
            with pdfium.PdfDocument.new() as empty:
                empty.save(source)
            with self.assertRaises(OCRInputError):
                inspect_input(str(source), root, "empty.pdf")

    def test_pipeline_config_defaults_overrides_and_validation(self):
        self.assertEqual(PipelineSettings(), PipelineSettings(300, 4, 1, 4))
        config = "ollama: {base_url: http://localhost, model: ocr, timeout_seconds: 10}\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "config.yaml")
            path.write_text(config + "pipeline: {pdf_dpi: 144, llm_workers: 2}\n")
            self.assertEqual(load_settings(path).pipeline, PipelineSettings(144, 4, 2, 4))
            for value in ("0", "-1", "true", "1.5", "null", "'300'"):
                path.write_text(config + f"pipeline: {{pdf_dpi: {value}}}\n")
                with self.assertRaises(ConfigError):
                    load_settings(path)


class FakeClient:
    def __init__(self, recognize=None):
        self.recognize_function = recognize
        self.calls = []
        self.closed = False

    def recognize(self, data):
        with Image.open(BytesIO(data)) as image:
            page = image.getpixel((0, 0))[0]
        self.calls.append((threading.get_ident(), page))
        if self.recognize_function:
            return self.recognize_function(page)
        return f"page {page}\nimage[[0,0,999,999]]"

    def close(self):
        self.closed = True


class ControlledPipeline(Pipeline):
    """Inject inexpensive thread workers; retain the actual coordinator and exports."""

    def __init__(self, count=6, settings=None, prepare=None, post=None, client_factory=None, **kwargs):
        self.count = count
        self.prepare_hook = prepare
        self.post_hook = post
        self.transitions = []
        self.max_materialized = 0
        settings = settings or OllamaSettings("http://unused", "ocr", 10)
        super().__init__(settings, image_executor=ThreadPoolExecutor(settings.pipeline.image_workers),
                         post_executor=ThreadPoolExecutor(settings.pipeline.postprocess_workers),
                         client_factory=client_factory or FakeClient, **kwargs)

    def _transition(self, job, task, status, error=None):
        super()._transition(job, task, status, error)
        counts = {}
        for stage in self.limits:
            tasks = [t for j in self.jobs.values() for t in j.plan.tasks.values() if t.stage == stage]
            counts[stage] = (sum(t.status == "running" for t in tasks),
                             sum(t.status in {"running", "ready"} for t in tasks))
        self.transitions.append((job.id, task.id, status, counts))
        self.max_materialized = max(self.max_materialized,
            sum(len(list(j.root.glob("prepared-*.png"))) for j in self.jobs.values()))

    def _operation(self, job, task):
        if task.id == "inspect":
            return lambda: (True, self.count), ()
        if task.page and task.stage == "image":
            def prepare():
                if self.prepare_hook:
                    self.prepare_hook(task.page)
                path = job.root / f"prepared-{task.page}.png"
                with Image.new("RGB", (10, 8), (task.page, 0, 0)) as image:
                    image.save(path)
                return str(path)
            return prepare, ()
        if task.page and task.stage == "postprocess" and self.post_hook:
            def post():
                self.post_hook(task.page)
                return postprocess_page(str(job.root), task.page, True)
            return post, ()
        return super()._operation(job, task)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def make_pipeline(self, **kwargs):
        pipeline = ControlledPipeline(**kwargs)
        self.addAsyncCleanup(pipeline.close)
        return pipeline

    async def wait_job(self, job):
        await asyncio.wait_for(job.done.wait(), 10)

    async def test_overlap_out_of_order_completion_and_ordered_crops(self):
        release_first = threading.Event()
        self.addCleanup(release_first.set)
        recognized_second = threading.Event()

        def prepare(page):
            if page == 1 and not release_first.wait(5):
                raise RuntimeError("stage overlap did not occur")

        def recognize(page):
            if page == 2:
                recognized_second.set()
                release_first.set()
            return f"body {page}\nimage[[0,0,999,999]]"

        client = FakeClient(recognize)
        pipeline = await self.make_pipeline(count=4, prepare=prepare, client_factory=lambda: client)
        job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
        await self.wait_job(job)
        self.assertTrue(recognized_second.is_set())
        self.assertEqual(job.status, "completed")
        text = Path(job.result.directory, "document.md").read_text()
        self.assertEqual([text.index(f"<!-- Page {n} -->") for n in range(1, 5)],
                         sorted(text.index(f"<!-- Page {n} -->") for n in range(1, 5)))
        for page in range(1, 5):
            name = f"page-{page:04d}-image-001.png"
            self.assertIn(name, text)
            with Image.open(Path(job.result.directory, name)) as crop:
                self.assertEqual(crop.getpixel((0, 0)), (page, 0, 0))
        self.assertFalse(list(job.root.glob("prepared-*")))
        self.assertEqual(len(client.calls), 4)
        starts = [tid for _, tid, status, _ in pipeline.transitions if status == "running"]
        self.assertLess(starts.index("page-2:llm"), starts.index("page-1:llm"))

    async def test_global_limits_fairness_backpressure_and_thread_owned_clients(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        clients = []

        def factory():
            client = FakeClient(lambda p: (gate.wait(5), f"page {p}")[1])
            clients.append(client)
            return client

        settings = OllamaSettings("http://unused", "ocr", 10, PipelineSettings(72, 4, 2, 4))
        pipeline = await self.make_pipeline(count=40, settings=settings, client_factory=factory)
        first = await pipeline.submit(BytesIO(b"a"), "a.pdf")
        second = await pipeline.submit(BytesIO(b"b"), "b.pdf")
        # Wait for LLM workers to block, then allow upstream buffers to settle.
        async def blocked():
            while sum(len(c.calls) for c in clients) < 2:
                await asyncio.sleep(.001)
        await asyncio.wait_for(blocked(), 3)
        await asyncio.sleep(.05)
        self.assertLess(pipeline.max_materialized, 40)
        gate.set()
        await asyncio.gather(self.wait_job(first), self.wait_job(second))
        for _, _, _, counts in pipeline.transitions:
            for stage, (running, admitted) in counts.items():
                self.assertLessEqual(running, pipeline.limits[stage])
                self.assertLessEqual(admitted, 2 * pipeline.limits[stage])
        llm_jobs = [jid for jid, tid, state, _ in pipeline.transitions
                    if tid.endswith(":llm") and state == "running"]
        self.assertIn(second.id, llm_jobs[:8])
        self.assertIn(first.id, llm_jobs[:8])
        self.assertEqual(sum(len(c.calls) for c in clients), 80)
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(len({thread for thread, _ in c.calls}) == 1 for c in clients))

    async def test_partial_failures_at_each_page_stage_and_all_failed(self):
        for stage in ("image", "llm", "postprocess"):
            with self.subTest(stage=stage):
                def fail(page):
                    if page == 2:
                        raise OCRBackendError("deliberate page failure")
                client = FakeClient(lambda page: (fail(page), f"body {page}")[1] if stage == "llm" else f"body {page}")
                pipeline = await self.make_pipeline(count=3, prepare=fail if stage == "image" else None,
                    post=fail if stage == "postprocess" else None, client_factory=lambda: client)
                job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
                await self.wait_job(job)
                self.assertEqual(job.status, "partial")
                text = Path(job.result.directory, "document.md").read_text()
                self.assertIn("<!-- Page 2 -->\n\n> OCR failed for this page during", text)
                self.assertEqual(job.result.failed_pages[0]["page"], 2)
                self.assertEqual(job.plan.tasks[f"page-2:{stage}"].status, "failed")
        client = FakeClient(lambda page: (_ for _ in ()).throw(OCRBackendError("offline")))
        pipeline = await self.make_pipeline(count=2, client_factory=lambda: client)
        job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
        await self.wait_job(job)
        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.result.archive)

    async def test_fatal_export_failure_cancellation_and_cleanup_wait_for_workers(self):
        pipeline = await self.make_pipeline(count=2)
        with patch("app.pipeline.archive_document", side_effect=OSError("disk full")):
            job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
            await self.wait_job(job)
        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.result.archive)
        gate = threading.Event()
        self.addCleanup(gate.set)
        started = threading.Event()
        def prepare(page):
            started.set()
            gate.wait(5)
        blocked = await self.make_pipeline(count=6, prepare=prepare)
        job = await blocked.submit(BytesIO(b"pdf"), "book.pdf")
        await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        closing = asyncio.create_task(blocked.close())
        await asyncio.sleep(.02)
        self.assertTrue(job.root.exists())
        self.assertFalse(closing.done())
        gate.set()
        await asyncio.wait_for(closing, 5)
        self.assertFalse(job.root.exists())
        self.assertTrue(any(t.status == "cancelled" for t in job.plan.tasks.values()))

    async def test_stream_disconnect_replay_snapshot_heartbeat_and_expiry_pin(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        client = FakeClient(lambda page: (gate.wait(5), "content")[1])
        pipeline = await self.make_pipeline(count=1, client_factory=lambda: client)
        job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
        stream = pipeline.events(job, heartbeat=.01)
        self.assertIn("job.accepted", await anext(stream))
        # Closing only the subscriber must not cancel job execution.
        await stream.aclose()
        async def wait_recognition():
            while not client.calls:
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait_recognition(), 3)
        replay = pipeline.events(job, after=job.sequence, heartbeat=.01)
        self.assertEqual(await anext(replay), ": heartbeat\n\n")
        await replay.aclose()
        gate.set()
        await self.wait_job(job)
        for n in range(520):
            job.emit("test", index=n)
        stream = pipeline.events(job, after=1)
        snapshot = await anext(stream)
        self.assertIn("job.snapshot", snapshot)
        self.assertEqual(parse_events(snapshot)[0]["sequence"], job.sequence)
        await stream.aclose()
        # Pin before the expiry sweep, including response construction to first send.
        response = JobZipResponse(job)
        job.expires_at = time.time() - 1
        pipeline.expire()
        self.assertIn(job.id, pipeline.jobs)
        self.assertIsNone(pipeline.get(job.id))
        async def send(message):
            self.assertTrue(job.root.exists())
        await response({"type": "http", "method": "GET", "headers": [],
                        "asgi": {"spec_version": "2.4"}}, None, send)
        pipeline.expire()
        self.assertNotIn(job.id, pipeline.jobs)
        self.assertFalse(job.root.exists())

    async def test_asgi_stream_sends_before_completion_and_disconnect_keeps_job(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        client = FakeClient(lambda page: (gate.wait(5), "recognized")[1])
        pipeline = await self.make_pipeline(count=1, client_factory=lambda: client)
        upload = UploadFile(BytesIO(b"pdf"), filename="book.pdf")
        response = await stream_ocr(upload, pipeline)
        # This direct ASGI harness avoids TestClient's response buffering.
        disconnected = asyncio.Event()
        bodies = []
        job = next(iter(pipeline.jobs.values()))
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                self.assertFalse(job.done.is_set())
                bodies.append(message["body"].decode())
                disconnected.set()
        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}
        await asyncio.wait_for(response({"type": "http", "method": "POST", "headers": [],
                                        "asgi": {"spec_version": "2.0"}}, receive, send), 3)
        self.assertIn("job.accepted", "".join(bodies))
        self.assertFalse(job.cancel_requested)
        gate.set()
        await self.wait_job(job)
        self.assertEqual(job.status, "completed")

    async def test_assembly_and_broken_pool_failures_publish_no_archive(self):
        from concurrent.futures.process import BrokenProcessPool
        for target, error in (("assemble_document", OSError("disk full")),
                              ("inspect_input", BrokenProcessPool("worker exited"))):
            pipeline = await self.make_pipeline(count=2)
            # ControlledPipeline overrides inspection, so use a real image worker
            # operation for the broken-pool path while retaining thread executors.
            if target == "inspect_input":
                original = pipeline._operation
                pipeline._operation = lambda job, task: (
                    (lambda: (_ for _ in ()).throw(error)), ()) if task.id == "inspect" else original(job, task)
                job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
                await self.wait_job(job)
            else:
                with patch(f"app.pipeline.{target}", side_effect=error):
                    job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
                    await self.wait_job(job)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.result.status, "failed")
            self.assertIsNone(job.result.archive)

    async def test_failed_jobs_do_not_consume_future_page_window(self):
        def fail(page):
            raise OSError("render workspace unavailable")
        pipeline = await self.make_pipeline(count=40, prepare=fail)
        failed = await pipeline.submit(BytesIO(b"pdf"), "failed.pdf")
        await self.wait_job(failed)
        self.assertEqual(failed.status, "failed")
        pipeline.prepare_hook = None
        pipeline.count = 2
        later = await pipeline.submit(BytesIO(b"pdf"), "later.pdf")
        await self.wait_job(later)
        self.assertEqual(later.status, "completed")

    async def test_shutdown_waits_for_active_download_and_failed_send_unpins(self):
        pipeline = await self.make_pipeline(count=1)
        job = await pipeline.submit(BytesIO(b"pdf"), "book.pdf")
        await self.wait_job(job)
        response = JobZipResponse(job)
        closing = asyncio.create_task(pipeline.close())
        await asyncio.sleep(.02)
        self.assertFalse(closing.done())
        self.assertTrue(job.root.exists())
        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("disconnected")
        with self.assertRaises(OSError):
            await response({"type": "http", "method": "GET", "headers": [],
                            "asgi": {"spec_version": "2.4"}}, None, send)
        await asyncio.wait_for(closing, 3)
        self.assertEqual(job.downloads, 0)
        self.assertFalse(job.root.exists())


class APIAndCLITests(unittest.TestCase):
    def test_real_spawned_pdf_pipeline_stream_status_reconnect_and_repeat_download(self):
        raw = "text\nimage[[0,0,999,999]]"
        with TestClient(app) as client, patch.object(OllamaOCRClient, "recognize", return_value=raw) as recognize:
            response = client.post("/api/ocr/stream", files={"file": ("book.pdf", pdf_bytes())},
                                   data={"pdf_dpi": "72"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            events = parse_events(response.text)
            self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(recognize.call_count, 3)
            job_id = events[0]["job_id"]
            snapshot = client.get(f"/api/ocr/jobs/{job_id}").json()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["stats"]["pages_completed"], 3)
            download = snapshot["result"]["download_url"]
            first, second = client.get(download), client.get(download)
            self.assertEqual(first.content, second.content)
            from zipfile import ZipFile
            with ZipFile(BytesIO(first.content)) as archive:
                markdown = next(n for n in archive.namelist() if n.endswith("document.md"))
                self.assertEqual(archive.read(markdown).count(b"<!-- Page "), 3)
                self.assertEqual(len(archive.namelist()), 4)
            replay = client.get(f"/api/ocr/jobs/{job_id}/events", headers={"Last-Event-ID": "1"})
            self.assertEqual(parse_events(replay.text)[0]["sequence"], 2)
            self.assertEqual(client.get(f"/api/ocr/jobs/{job_id}/events",
                                       headers={"Last-Event-ID": "bad"}).status_code, 400)
            self.assertEqual(client.get("/api/ocr/jobs/unknown").status_code, 404)
            for value in ("0", "-1", "abc"):
                self.assertEqual(client.post("/api/ocr/stream", files={"file": ("a.pdf", pdf_bytes(1))},
                                             data={"pdf_dpi": value}).status_code, 422)

    def test_cli_partial_exit_and_worker_overrides(self):
        counter = 0
        def recognize(data):
            nonlocal counter
            counter += 1
            if counter == 2:
                raise OCRBackendError("page failed")
            return "recognized"
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "book.pdf")
            source.write_bytes(pdf_bytes())
            stdout, stderr = StringIO(), StringIO()
            with patch.object(OllamaOCRClient, "recognize", side_effect=recognize):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["ocr", str(source), "--output-path", root, "--pdf-dpi", "72",
                                 "--image-workers", "2", "--llm-workers", "1", "--postprocess-workers", "2"])
            self.assertEqual(code, 2)
            self.assertIn("llm=", stderr.getvalue())
            markdown = Path(stdout.getvalue().strip(), "document.md").read_text()
            self.assertEqual(markdown.count("<!-- Page "), 3)
            self.assertIn("page failed", markdown)

    def test_invalid_streaming_input_reports_terminal_failure(self):
        with TestClient(app) as client:
            response = client.post("/api/ocr/stream", files={"file": ("bad.pdf", b"%PDF-broken")})
            events = parse_events(response.text)
            self.assertEqual(events[-1]["result"]["status"], "failed")
            self.assertIsNone(events[-1]["result"]["download_url"])
            self.assertEqual(client.get(f"/api/ocr/jobs/{events[0]['job_id']}/download").status_code, 409)


if __name__ == "__main__":
    unittest.main()
