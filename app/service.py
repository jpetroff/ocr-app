"""Shared pipeline lifecycle and the legacy single-image service helper."""

from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import BinaryIO

from .config import OCRSettings
from .document import OCRDocument, build_document
from .ocr_client import OCRClient, create_ocr_client
from .processing import prepare_image
from .pipeline import Pipeline


@asynccontextmanager
async def create_pipeline(settings: OCRSettings):
    pipeline = Pipeline(settings)
    try:
        yield pipeline
    finally:
        await pipeline.close()


class OCRService:
    def __init__(self, client: OCRClient) -> None:
        self.client = client

    def ocr(self, file: BinaryIO, filename: str) -> OCRDocument:
        """Prepare one image, recognize its blocks, and extract image crops."""
        image_bytes = prepare_image(file, filename)
        return build_document(self.client.recognize(image_bytes), image_bytes)


@contextmanager
def create_service(settings: OCRSettings) -> Iterator[OCRService]:
    client = create_ocr_client(settings)
    try:
        yield OCRService(client)
    finally:
        client.close()
