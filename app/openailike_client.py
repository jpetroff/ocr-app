"""Nonstreaming llama.cpp OCR using OpenAI-compatible chat completions."""

import base64
from io import BytesIO

import httpx
from PIL import Image

from .config import OCRSettings
from .errors import OCRBackendError, OCRTimeoutError


class OpenAILikeOCRClient:
    def __init__(self, settings: OCRSettings) -> None:
        self.settings = settings
        base_url = settings.base_url.rstrip("/")
        self.url = base_url + ("/chat/completions" if base_url.endswith("/v1")
                               else "/v1/chat/completions")
        self.client = httpx.Client(timeout=settings.timeout_seconds)

    def recognize(self, image_bytes: bytes) -> str:
        """Send prepared image bytes with their actual MIME type; preserve raw text."""
        with Image.open(BytesIO(image_bytes)) as image:
            mime = Image.MIME[image.format]
        image_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        payload = {
            "model": self.settings.model,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": self.settings.prompt},
                ],
            }],
        }
        try:
            response = self.client.post(self.url, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise OCRTimeoutError("OpenAI like OCR request timed out.") from exc
        except httpx.HTTPError as exc:
            raise OCRBackendError(f"OpenAI like OCR request failed: {exc}") from exc
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OCRBackendError("OpenAI like returned an invalid response.") from exc
        if not isinstance(content, str):
            raise OCRBackendError("OpenAI like returned no generated text.")
        return content

    def close(self) -> None:
        self.client.close()
