"""Image conversion contract; conversion is intentionally unimplemented.

Decode common raster images with Pillow and register pillow-heif for HEIC/HEIF.
Apply EXIF orientation, composite transparency onto white, and encode RGB PNG.
Yield every TIFF page, but only the first frame of animated images. Reject
unsupported input clearly; SVG and camera RAW are outside this contract.

Render PDFs sequentially at 200 DPI with pypdfium2, closing page/bitmap/document
resources promptly. Protect all PDFium operations with a process-wide lock:
PDFium must not be called concurrently by FastAPI worker threads, even for
different documents. Yield pages individually instead of buffering a whole PDF.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from .errors import OCRNotImplementedError


@dataclass(frozen=True)
class ImagePage:
    page_number: int  # One-based, in source order.
    png_bytes: bytes


def iter_pages(file: BinaryIO, filename: str) -> Iterator[ImagePage]:
    """Read from the current stream position and yield normalized PNG pages."""
    raise OCRNotImplementedError("Image and PDF conversion is not implemented yet.")
