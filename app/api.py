"""FastAPI adapter; application liveness does not depend on Ollama."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.types import Receive, Scope, Send

from .config import load_settings
from .errors import OCRBackendError, OCRInputError, OCRTimeoutError
from .export import archive_document, export_document
from .service import OCRService, create_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    with create_service(load_settings()) as service:
        app.state.ocr_service = service
        yield


app = FastAPI(title="Document OCR", lifespan=lifespan)


def get_service(request: Request) -> OCRService:
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
def ocr(
    file: UploadFile,
    service: Annotated[OCRService, Depends(get_service)],
) -> FileResponse:
    try:
        filename = file.filename or "upload"
        document = service.ocr(file.file, filename)
        workspace = TemporaryDirectory(prefix="ocr-", dir="/tmp")
        try:
            directory = export_document(document, Path(workspace.name), filename)
            archive = Path(workspace.name) / f"{directory.name}.zip"
            archive_document(directory, archive)
            return TemporaryZipResponse(archive, workspace)
        except BaseException:
            workspace.cleanup()
            raise
    except OCRInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OCRTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except OCRBackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not export the OCR document.") from exc
