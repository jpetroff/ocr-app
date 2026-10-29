"""Nonstreaming Ollama OCR; return generated text without postprocessing."""

from httpx import HTTPError, TimeoutException
from ollama import Client, ResponseError

from .config import OCRSettings
from .errors import OCRBackendError, OCRTimeoutError


class OllamaOCRClient:
    def __init__(self, settings: OCRSettings) -> None:
        self.settings = settings
        # Construction does not contact Ollama or download a model.
        self.client = Client(host=settings.base_url, timeout=settings.timeout_seconds)

    def recognize(self, image_bytes: bytes) -> str:
        """Send one image using the SDK's base64 encoding and preserve raw text."""
        try:
            response = self.client.generate(
                model=self.settings.model,
                prompt="<|grounding|>Convert the document to markdown.",
                images=[image_bytes],
                stream=False,
            )
        except TimeoutException as exc:
            raise OCRTimeoutError("Ollama OCR request timed out.") from exc
        except (ConnectionError, HTTPError, ResponseError) as exc:
            raise OCRBackendError(f"Ollama OCR request failed: {exc}") from exc
        except ValueError as exc:
            raise OCRBackendError("Ollama returned an invalid response.") from exc
        if not isinstance(response.response, str):
            raise OCRBackendError("Ollama returned no generated text.")
        return response.response

    def close(self) -> None:
        self.client.close()
