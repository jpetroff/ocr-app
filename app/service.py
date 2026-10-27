"""Shared pipeline lifecycle and the legacy single-image service helper."""

from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import BinaryIO

from .config import OllamaSettings
from .document import OCRDocument, build_document
from .ollama_client import OllamaOCRClient
from .processing import prepare_image
from .pipeline import Pipeline


@asynccontextmanager
async def create_pipeline(settings: OllamaSettings):
    pipeline = Pipeline(settings)
    try:
        yield pipeline
    finally:
        await pipeline.close()


class OCRService:
    def __init__(self, client: OllamaOCRClient) -> None:
        self.client = client

    def ocr(self, file: BinaryIO, filename: str) -> OCRDocument:
        """Prepare one image, recognize its blocks, and extract image crops."""
        image_bytes = prepare_image(file, filename)
        return build_document(self.client.recognize(image_bytes), image_bytes)


@contextmanager
def create_service(settings: OllamaSettings) -> Iterator[OCRService]:
    client = OllamaOCRClient(settings)
    try:
        yield OCRService(client)
    finally:
        client.close()
