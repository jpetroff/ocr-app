"""Synchronous OCR core shared by CLI and API."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO

from .config import OllamaSettings
from .ollama_client import OllamaOCRClient
from .processing import iter_pages


@dataclass(frozen=True)
class OCRPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class OCRResult:
    pages: list[OCRPage]


class OCRService:
    def __init__(self, client: OllamaOCRClient) -> None:
        self.client = client

    def ocr(self, file: BinaryIO, filename: str) -> OCRResult:
        return OCRResult(pages=[
            OCRPage(page_number=page.page_number, text=self.client.recognize(page.png_bytes))
            for page in iter_pages(file, filename)
        ])


@contextmanager
def create_service(settings: OllamaSettings) -> Iterator[OCRService]:
    client = OllamaOCRClient(settings)
    try:
        yield OCRService(client)
    finally:
        client.close()
