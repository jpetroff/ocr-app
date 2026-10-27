"""FastAPI adapter; application liveness does not depend on Ollama."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from .config import load_settings
from .errors import OCRBackendError, OCRInputError, OCRTimeoutError
from .pipeline import Job, Pipeline
from .service import create_pipeline


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with create_pipeline(load_settings()) as service:
        app.state.ocr_service = service
        yield


app = FastAPI(title="Document OCR", lifespan=lifespan)


def get_service(request: Request) -> Pipeline:
    return request.app.state.ocr_service


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class TemporaryZipResponse(FileResponse):
    """Own the workspace until transmission finishes, fails, or is cancelled."""

    def __init__(self, archive: Path, workspace: TemporaryDirectory) -> None:
        super().__init__(archive, media_type="application/zip", filename=archive.name)
        self.workspace = workspace

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Stream bytes ourselves: a pathsend extension may defer opening the file.
        scope = dict(scope, extensions={
            key: value for key, value in scope.get("extensions", {}).items()
            if key != "http.response.pathsend"
        })
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.workspace.cleanup()


@app.post(
    "/api/ocr", response_class=FileResponse,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def ocr(
    file: UploadFile,
    service: Annotated[Pipeline, Depends(get_service)],
    pdf_dpi: Annotated[int | None, Form(gt=0)] = None,
) -> FileResponse:
    job = None
    transferred = False
    try:
        job = await service.submit(file.file, file.filename or "upload", pdf_dpi)
        await job.done.wait()
        if job.error:
            raise job.error
        if not job.result or not job.result.archive:
            raise OCRBackendError("OCR did not produce a document.")
        # Blocking requests keep the original one-shot workspace ownership.
        service.jobs.pop(job.id)
        response = TemporaryZipResponse(Path(job.result.archive), job.workspace)
        response.headers["X-OCR-Status"] = job.status
        response.headers["X-OCR-Failed-Pages"] = str(sum(p.error is not None for p in job.pages.values()))
        transferred = True
        return response
    except OCRInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OCRTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except OCRBackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not export the OCR document.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="OCR processing failed.") from exc
    finally:
        if job and job.done.is_set() and not transferred:
            service.jobs.pop(job.id, None)
            job.workspace.cleanup()


class JobZipResponse(FileResponse):
    """Pin an artifact before returning the response, through the last body chunk."""

    def __init__(self, job: Job):
        self.job = job
        job.downloads += 1
        job.downloads_done.clear()
        super().__init__(job.result.archive, media_type="application/zip", filename=f"{Path(job.result.directory).name}.zip")

    async def __call__(self, scope, receive, send):
        scope = dict(scope, extensions={k: v for k, v in scope.get("extensions", {}).items()
                                       if k != "http.response.pathsend"})
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.job.downloads -= 1
            if not self.job.downloads:
                self.job.downloads_done.set()


def require_job(service: Pipeline, job_id: str) -> Job:
    job = service.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired.")
    return job


def event_response(service, job, after=0):
    return StreamingResponse(service.events(job, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ocr/stream", response_class=StreamingResponse)
async def stream_ocr(
    file: UploadFile,
    service: Annotated[Pipeline, Depends(get_service)],
    pdf_dpi: Annotated[int | None, Form(gt=0)] = None,
):
    job = await service.submit(file.file, file.filename or "upload", pdf_dpi)
    # The coordinator, not this generator, owns the execution and upload copy.
    return event_response(service, job)


@app.get("/api/ocr/jobs/{job_id}")
async def job_status(job_id: str, service: Annotated[Pipeline, Depends(get_service)]):
    return require_job(service, job_id).snapshot()


@app.get("/api/ocr/jobs/{job_id}/events", response_class=StreamingResponse)
async def job_events(job_id: str, request: Request, service: Annotated[Pipeline, Depends(get_service)]):
    try:
        after = int(request.headers.get("Last-Event-ID", "0"))
        if after < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Last-Event-ID must be a nonnegative integer.") from None
    return event_response(service, require_job(service, job_id), after)


@app.get("/api/ocr/jobs/{job_id}/download", response_class=FileResponse)
async def job_download(job_id: str, service: Annotated[Pipeline, Depends(get_service)]):
    job = require_job(service, job_id)
    if not job.done.is_set():
        raise HTTPException(409, "Document is still processing.")
    if not job.result or not job.result.archive:
        raise HTTPException(409, "Job has no downloadable document.")
    return JobZipResponse(job)
