"""Validate single images, preserving supported bytes or normalizing to PNG."""

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from .errors import OCRInputError

register_heif_opener(thumbnails=False)

_DIRECT_FORMATS = {"PNG", "JPEG", "WEBP"}
_INPUT_FORMATS = _DIRECT_FORMATS | {"BMP", "GIF", "TIFF", "HEIF", "AVIF"}


@contextmanager
def _open_image(image_bytes: bytes) -> Iterator[Image.Image]:
    """Decode and validate one raster image independently of its filename."""
    if not image_bytes:
        raise OCRInputError("The image is empty.")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format not in _INPUT_FORMATS:
                raise OCRInputError("Unsupported image format.")
            if getattr(image, "n_frames", 1) != 1:
                raise OCRInputError("Multi-frame images are not supported; supply one image.")
            image.load()
            yield image
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise OCRInputError("Input is not a valid supported image.") from exc


def prepare_image(file: BinaryIO, filename: str) -> bytes:
    """Read from the current stream position; inspect content before inference."""
    image_bytes = file.read()
    with _open_image(image_bytes) as image:
        if (
            image.format in _DIRECT_FORMATS
            and image.mode in {"RGB", "L"}
            and "transparency" not in image.info
            and image.getexif().get(274, 1) == 1
        ):
            return image_bytes
    return convert_image(image_bytes, filename)


def convert_image(image_bytes: bytes, filename: str) -> bytes:
    """Correct orientation, flatten transparency onto white, and encode RGB PNG."""
    with _open_image(image_bytes) as image, ImageOps.exif_transpose(image) as oriented:
        with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as rgb:
            with rgba.getchannel("A") as alpha:
                rgb.paste(rgba, mask=alpha)
            output = BytesIO()
            rgb.save(output, format="PNG")
            return output.getvalue()
