"""Errors shared by the OCR service and its adapters."""


class OCRNotImplementedError(NotImplementedError):
    """Raised while a processing or inference stage is still a stub."""
