"""Ollama adapter with an explicit inference stub.

Future inference sends one PNG per request with the configured model (default:
deepseek-ocr), prompt 'Free OCR.', and stream=False, returning recognized text.
DeepSeek-OCR requires Ollama 0.13.0 or later:
https://ollama.com/library/deepseek-ocr
"""

from ollama import Client

from .config import OllamaSettings
from .errors import OCRNotImplementedError


class OllamaOCRClient:
    def __init__(self, settings: OllamaSettings) -> None:
        self.settings = settings
        # Construction does not contact Ollama or download a model.
        self.client = Client(host=settings.base_url, timeout=settings.timeout_seconds)

    def recognize(self, png_bytes: bytes) -> str:
        raise OCRNotImplementedError("Ollama OCR inference is not implemented yet.")

    def close(self) -> None:
        self.client.close()
