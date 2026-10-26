"""FastAPI adapter; application liveness does not depend on Ollama."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile

from .config import load_settings
from .errors import OCRNotImplementedError
from .service import OCRResult, OCRService, create_service


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


@app.post("/api/ocr", response_model=OCRResult)
def ocr(
    file: UploadFile,
    service: Annotated[OCRService, Depends(get_service)],
) -> OCRResult:
    try:
        return service.ocr(file.file, file.filename or "upload")
    except OCRNotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
