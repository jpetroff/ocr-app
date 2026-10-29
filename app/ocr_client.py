"""Provider-neutral OCR contract and client construction."""

from typing import Protocol

from .config import ConfigError, OCRSettings
from .ollama_client import OllamaOCRClient
from .openailike_client import OpenAILikeOCRClient


class OCRClient(Protocol):
    def recognize(self, image_bytes: bytes) -> str: ...

    def close(self) -> None: ...


def create_ocr_client(settings: OCRSettings) -> OCRClient:
    if settings.provider == "ollama":
        return OllamaOCRClient(settings)
    if settings.provider == "openailike":
        return OpenAILikeOCRClient(settings)
    raise ConfigError(f"Unsupported OCR provider: {settings.provider!r}.")
