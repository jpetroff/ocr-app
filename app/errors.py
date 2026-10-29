"""Errors shared by the OCR service and its adapters."""


class OCRError(Exception):
    """An expected OCR failure suitable for a concise adapter response."""


class OCRInputError(OCRError):
    """The input cannot be processed as a supported image or readable PDF."""


class OCRBackendError(OCRError):
    """The OCR backend is unavailable or failed to return generated text."""


class OCRTimeoutError(OCRBackendError):
    """The OCR backend did not respond within the configured timeout."""


class OCROutputError(OCRBackendError):
    """Generated grounding is malformed or cannot describe a usable crop."""
