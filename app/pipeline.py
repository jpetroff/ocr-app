"""Disk-backed page workers and a single-owner, fair asynchronous job coordinator."""

import asyncio
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

from .config import OllamaSettings, PipelineSettings
from .document import build_document
from .errors import OCRBackendError, OCRInputError
from .export import archive_document, render_markdown
from .ollama_client import OllamaOCRClient
from .processing import prepare_image


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRecord:
    id: str
    stage: str
    page: int | None = None
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    _started: float = field(default=0, repr=False)

    def snapshot(self):
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}


@dataclass
class JobPlan:
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    page_count: int = 0
    is_pdf: bool = False


@dataclass
class PageResult:
    page: int
    markdown: str | None = None
    images: list[str] = field(default_factory=list)
    error: str | None = None
    failed_stage: str | None = None


@dataclass
class JobResult:
    status: str
    directory: str | None = None
    archive: str | None = None
    failed_pages: list[dict] = field(default_factory=list)


def inspect_input(source: str, root: str, filename: str) -> tuple[bool, int]:
    with open(source, "rb") as stream:
        header = stream.read(1024)
        stream.seek(0)
        if b"%PDF-" not in header:
            # Preserve existing raster validation, orientation and direct-format bytes.
            prepared = prepare_image(stream, filename)
            Path(root, "prepared-1.png").write_bytes(prepared)
            return False, 1
    import pypdfium2 as pdfium
    try:
        with pdfium.PdfDocument(source) as pdf:
            count = len(pdf)
            if not count:
                raise OCRInputError("PDF contains no pages.")
            return True, count
    except pdfium.PdfiumError as exc:
        raise OCRInputError("Input is not a readable PDF (encrypted PDFs require an unlocked copy).") from exc


def prepare_page(source: str, root: str, page: int, dpi: int, is_pdf: bool) -> str:
    destination = Path(root, f"prepared-{page}.png")
    if not is_pdf:
        return str(destination)
    import pypdfium2 as pdfium
    try:
        with pdfium.PdfDocument(source) as pdf:
            pdf.init_forms()
            with closing(pdf[page - 1]) as pdf_page:
                with closing(pdf_page.render(scale=dpi / 72, fill_color=(255, 255, 255, 255))) as bitmap:
                    with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                        rgb.save(destination, format="PNG")
    except pdfium.PdfiumError as exc:
        raise OCRInputError(f"Could not render PDF page {page}.") from exc
    return str(destination)


def postprocess_page(root: str, page: int, is_pdf: bool) -> PageResult:
    workspace = Path(root)
    image = workspace / f"prepared-{page}.png"
    with (workspace / f"raw-{page}.txt").open(encoding="utf-8", newline="") as raw:
        document = build_document(raw.read(), image.read_bytes())
    directory = workspace / f"page-{page}"
    directory.mkdir()
    images = []
    for block in document.blocks:
        for index, crop in enumerate(block.images):
            name = f"page-{page:04d}-{crop.filename}" if is_pdf else crop.filename
            block.images[index] = replace(crop, filename=name)
            (directory / name).write_bytes(crop.data)
            images.append(name)
    markdown = directory / "page.md"
    markdown.write_text(render_markdown(document), encoding="utf-8")
    image.unlink(missing_ok=True)
    (workspace / f"raw-{page}.txt").unlink(missing_ok=True)
    return PageResult(page, str(markdown), images)


def assemble_document(root: str, filename: str, is_pdf: bool, pages: list[PageResult]) -> str:
    basename = re.split(r"[/\\]", filename)[-1]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(basename).stem).strip("-_")[:80] or "document"
    directory = Path(root, f"{stem}-{uuid4().hex[:8]}")
    directory.mkdir()
    try:
        with (directory / "document.md").open("w", encoding="utf-8", newline="") as output:
            for index, page in enumerate(sorted(pages, key=lambda item: item.page)):
                if is_pdf:
                    if index:
                        output.write("\n\n")
                    output.write(f"<!-- Page {page.page} -->\n\n")
                if page.error:
                    # Keep errors on one quoted line; do not inject model/exception markup.
                    message = " ".join(page.error.split())
                    output.write(f"> OCR failed for this page during {page.failed_stage}: {message}")
                else:
                    with open(page.markdown, encoding="utf-8", newline="") as content:
                        shutil.copyfileobj(content, output)
                    for name in page.images:
                        shutil.copyfile(Path(page.markdown).parent / name, directory / name)
        return str(directory)
    except BaseException:
        shutil.rmtree(directory)
        raise


class Job:
    def __init__(self, workspace, filename, dpi):
        self.id = uuid4().hex
        self.workspace = workspace
        self.root = Path(workspace.name)
        self.filename = filename
        self.dpi = dpi
        self.plan = JobPlan({"inspect": TaskRecord("inspect", "image")})
        self.pages: dict[int, PageResult] = {}
        self.status = "running"
        self.result: JobResult | None = None
        self.error: Exception | None = None
        self.sequence = 0
        self.events = deque(maxlen=512)
        self.changed = asyncio.Event()
        self.done = asyncio.Event()
        self.expires_at: float | None = None
        self.downloads = 0
        self.downloads_done = asyncio.Event()
        self.downloads_done.set()
        self.cancel_requested = False

    def stats(self):
        stages = {}
        for task in self.plan.tasks.values():
            counts = stages.setdefault(task.stage, {})
            counts[task.status] = counts.get(task.status, 0) + 1
        return {"stages": stages, "pages_total": self.plan.page_count,
                "pages_completed": sum(p.error is None for p in self.pages.values()),
                "pages_failed": sum(p.error is not None for p in self.pages.values())}

    def result_data(self):
        if self.result is None:
            return None
        return {"status": self.result.status, "failed_pages": self.result.failed_pages,
                "download_url": f"/api/ocr/jobs/{self.id}/download" if self.result.archive else None,
                "expires_at": datetime.fromtimestamp(self.expires_at, timezone.utc).isoformat()
                if self.expires_at else None}

    def snapshot(self):
        return {"job_id": self.id, "sequence": self.sequence, "status": self.status,
                "stats": self.stats(), "tasks": [t.snapshot() for t in self.plan.tasks.values()],
                "result": self.result_data(), "error": str(self.error) if self.error else None}

    def emit(self, event, **details):
        self.sequence += 1
        item = {"sequence": self.sequence, "job_id": self.id, "timestamp": timestamp(),
                "stats": self.stats(), **details}
        self.events.append((event, item))
        self.changed.set()


class Pipeline:
    """All task state is owned by the event loop; executors only touch job files."""

    def __init__(self, settings: OllamaSettings, *, image_executor=None, post_executor=None,
                 client_factory=None, retention_seconds=3600):
        self.settings = settings
        self.limits = {"image": settings.pipeline.image_workers, "llm": settings.pipeline.llm_workers,
                       "postprocess": settings.pipeline.postprocess_workers}
        context = multiprocessing.get_context("spawn")
        self.executors = {
            "image": image_executor or ProcessPoolExecutor(self.limits["image"], mp_context=context),
            "llm": ThreadPoolExecutor(self.limits["llm"], thread_name_prefix="ocr-llm"),
            "postprocess": post_executor or ProcessPoolExecutor(self.limits["postprocess"], mp_context=context),
        }
        self.client_factory = client_factory or (lambda: OllamaOCRClient(settings))
        self.local = threading.local()
        self.clients = []
        self.jobs: dict[str, Job] = {}
        self.active = {}
        self.wake = asyncio.Event()
        self.accepting = True
        self.retention_seconds = retention_seconds
        self.cursor = {stage: None for stage in self.limits}
        self.runner = asyncio.create_task(self._run())

    async def submit(self, file, filename, dpi=None) -> Job:
        if not self.accepting:
            raise RuntimeError("OCR service is shutting down.")
        dpi = self.settings.pipeline.pdf_dpi if dpi is None else dpi
        PipelineSettings(pdf_dpi=dpi)  # Reuse strict positive-integer validation.
        workspace = TemporaryDirectory(prefix="ocr-", dir="/tmp")
        try:
            # UploadFile is request-owned: copy it completely before dispatching work.
            copying = asyncio.create_task(asyncio.to_thread(self._spool, file, Path(workspace.name, "input")))
            try:
                await asyncio.shield(copying)
            except asyncio.CancelledError:
                # A running filesystem operation cannot be cancelled by asyncio.
                # Keep both the request stream and workspace alive until it exits.
                await copying
                raise
            if not self.accepting:
                raise RuntimeError("OCR service is shutting down.")
            job = Job(workspace, filename, dpi)
            self.jobs[job.id] = job
            job.emit("job.accepted")
            self.wake.set()
            return job
        except BaseException:
            workspace.cleanup()
            raise

    @staticmethod
    def _spool(file, destination):
        with destination.open("wb") as output:
            shutil.copyfileobj(file, output)

    def _recognize(self, root, page):
        if not hasattr(self.local, "client"):
            self.local.client = self.client_factory()
            self.clients.append(self.local.client)
        content = self.local.client.recognize(Path(root, f"prepared-{page}.png").read_bytes())
        Path(root, f"raw-{page}.txt").write_text(content, encoding="utf-8", newline="")

    def _transition(self, job, task, status, error=None):
        task.status = status
        task.error = str(error) if error else None
        if status == "running":
            task.started_at, task._started = timestamp(), time.monotonic()
        elif status in {"completed", "failed", "skipped", "cancelled"}:
            task.finished_at = timestamp()
            task.duration_seconds = time.monotonic() - task._started if task.started_at else 0
        job.emit("task.updated", task=task.snapshot())

    def _plan(self, job, is_pdf, count):
        job.plan.is_pdf, job.plan.page_count = is_pdf, count
        for page in range(1, count + 1):
            dependency = "inspect"
            for stage in ("image", "llm", "postprocess"):
                name = f"page-{page}:{stage}"
                job.plan.tasks[name] = TaskRecord(name, stage, page, [dependency])
                dependency = name
        job.plan.tasks["assemble"] = TaskRecord("assemble", "postprocess", dependencies=[
            f"page-{p}:postprocess" for p in range(1, count + 1)])
        job.plan.tasks["archive"] = TaskRecord("archive", "postprocess", dependencies=["assemble"])
        job.emit("job.planned", pages=count, is_pdf=is_pdf)

    def _eligible(self, job, task):
        if job.error or job.cancel_requested or task.status != "pending":
            return False
        if task.id == "assemble":
            return len(job.pages) == job.plan.page_count
        if not all(job.plan.tasks[d].status == "completed" for d in task.dependencies):
            return False
        if task.stage == "image" and task.page:
            # A fixed global page window also bounds completed upstream artifacts
            # waiting for admission to a slower downstream stage.
            outstanding = sum(1 for j in self.jobs.values()
                              if not j.done.is_set() and not j.error and not j.cancel_requested
                              for t in j.plan.tasks.values()
                              if t.page and t.stage == "image" and t.status in {"ready", "running", "completed"}
                              and t.page not in j.pages)
            return outstanding < 2 * sum(self.limits.values())
        return True

    def _fair_task(self, stage):
        jobs = list(self.jobs.values())
        ids = [j.id for j in jobs]
        previous = self.cursor[stage]
        if previous in ids:
            offset = ids.index(previous) + 1
            jobs = jobs[offset:] + jobs[:offset]
        for job in jobs:
            for task in job.plan.tasks.values():
                if task.stage == stage and task.status == "ready":
                    self.cursor[stage] = job.id
                    return job, task
        return None

    def _admit(self, stage):
        count = sum(t.status in {"ready", "running"} for j in self.jobs.values()
                    for t in j.plan.tasks.values() if t.stage == stage)
        while count < 2 * self.limits[stage]:
            jobs = list(self.jobs.values())
            ids = [j.id for j in jobs]
            previous = self.cursor[stage]
            if previous in ids:
                offset = ids.index(previous) + 1
                jobs = jobs[offset:] + jobs[:offset]
            found = False
            for job in jobs:
                candidate = next((t for t in job.plan.tasks.values()
                                  if t.stage == stage and self._eligible(job, t)), None)
                if candidate:
                    self._transition(job, candidate, "ready")
                    self.cursor[stage] = job.id
                    count += 1
                    found = True
                    break
            if not found:
                break

    def _operation(self, job, task):
        root = str(job.root)
        if task.id == "inspect":
            return inspect_input, (str(job.root / "input"), root, job.filename)
        if task.id == "assemble":
            if not any(p.error is None for p in job.pages.values()):
                raise job.error or OCRBackendError("OCR failed for every page.")
            return assemble_document, (root, job.filename, job.plan.is_pdf, list(job.pages.values()))
        if task.id == "archive":
            return archive_document, (Path(job.result.directory), job.root / "document.zip")
        if task.stage == "image":
            return prepare_page, (str(job.root / "input"), root, task.page, job.dpi, job.plan.is_pdf)
        if task.stage == "llm":
            return self._recognize, (root, task.page)
        return postprocess_page, (root, task.page, job.plan.is_pdf)

    async def _execute(self, job, task):
        try:
            function, arguments = self._operation(job, task)
            result = await asyncio.get_running_loop().run_in_executor(
                self.executors[task.stage], function, *arguments)
            self._transition(job, task, "completed")
            if job.cancel_requested or job.error:
                return
            if task.id == "inspect":
                self._plan(job, *result)
            elif task.id == "assemble":
                job.result = JobResult("partial" if any(p.error for p in job.pages.values()) else "completed",
                                       directory=result)
            elif task.id == "archive":
                job.result.archive = str(job.root / "document.zip")
                self._finish(job, job.result.status)
            elif task.stage == "postprocess":
                job.pages[task.page] = result
                job.emit("page.completed", page=asdict(result) | {"markdown": None})
        except Exception as exc:
            self._transition(job, task, "failed", exc)
            if task.page and not isinstance(exc, (BrokenProcessPool, OSError)):
                job.pages[task.page] = PageResult(task.page, error=str(exc), failed_stage=task.stage)
                for downstream in job.plan.tasks.values():
                    if downstream.page == task.page and downstream.status in {"pending", "ready"}:
                        self._transition(job, downstream, "skipped")
                try:
                    (job.root / f"prepared-{task.page}.png").unlink(missing_ok=True)
                    (job.root / f"raw-{task.page}.txt").unlink(missing_ok=True)
                except OSError as cleanup_error:
                    job.error = cleanup_error
                job.emit("page.completed", page=asdict(job.pages[task.page]))
                # Preserve the original single-page error type for blocking HTTP mapping.
                if job.plan.page_count == 1:
                    job.error = exc
            else:
                job.error = exc
        finally:
            self.active.pop((job.id, task.id), None)
            self.wake.set()

    def _finish(self, job, status):
        job.status = status
        job.expires_at = time.time() + self.retention_seconds
        if job.result is None:
            job.result = JobResult(status)
        job.result.status = status
        job.result.failed_pages = [asdict(p) for p in job.pages.values() if p.error]
        job.emit(f"job.{status}", result=job.result_data(), error=str(job.error) if job.error else None)
        job.done.set()

    async def _run(self):
        while True:
            self.wake.clear()
            for job in list(self.jobs.values()):
                if not job.done.is_set() and (job.error or job.cancel_requested):
                    for task in job.plan.tasks.values():
                        if task.status in {"pending", "ready"}:
                            self._transition(job, task, "cancelled" if job.cancel_requested else "skipped")
                    if not any(key[0] == job.id for key in self.active):
                        self._finish(job, "failed")
            for stage, limit in self.limits.items():
                self._admit(stage)
                running = sum(j.plan.tasks[tid].stage == stage for (jid, tid) in self.active
                              for j in [self.jobs[jid]])
                while running < limit:
                    candidate = self._fair_task(stage)
                    if candidate is None:
                        break
                    job, task = candidate
                    self._transition(job, task, "running")
                    self.active[job.id, task.id] = asyncio.create_task(self._execute(job, task))
                    running += 1
            self.expire()
            if not self.accepting and not self.active:
                return
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    def expire(self):
        for job in list(self.jobs.values()):
            if job.done.is_set() and job.expires_at <= time.time() and not job.downloads:
                job.workspace.cleanup()
                del self.jobs[job.id]

    def get(self, job_id):
        self.expire()
        job = self.jobs.get(job_id)
        # Existing transmissions may pin expired files; they do not extend the
        # public lifetime or allow additional downloads after the deadline.
        if job and job.expires_at is not None and job.expires_at <= time.time():
            return None
        return job

    async def events(self, job, after=0, heartbeat=15):
        while True:
            # No await between reading history and clearing changed: no lost wakeup.
            job.changed.clear()
            history = list(job.events)
            if after > job.sequence or (history and after < history[0][1]["sequence"] - 1):
                snapshot = job.snapshot()
                after = job.sequence
                yield self._sse("job.snapshot", snapshot, after)
            else:
                for event, data in history:
                    if data["sequence"] > after:
                        after = data["sequence"]
                        yield self._sse(event, data, after)
            if job.done.is_set() and after >= job.sequence:
                return
            if after < job.sequence:
                continue
            try:
                await asyncio.wait_for(job.changed.wait(), heartbeat)
            except TimeoutError:
                yield ": heartbeat\n\n"

    @staticmethod
    def _sse(event, data, sequence):
        return f"id: {sequence}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def close(self):
        self.accepting = False
        for job in self.jobs.values():
            if not job.done.is_set():
                job.cancel_requested = True
        self.wake.set()
        await self.runner
        for executor in self.executors.values():
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        for client in self.clients:
            client.close()
        await asyncio.gather(*(job.downloads_done.wait() for job in self.jobs.values()))
        for job in self.jobs.values():
            job.workspace.cleanup()
        self.jobs.clear()
