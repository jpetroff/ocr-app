"""Parse grounded OCR into ordered blocks and extract binary PNG crops."""

from dataclasses import dataclass, field
from io import BytesIO
import json
import re

from PIL import Image

from .errors import OCROutputError


Coordinates = tuple[int, int, int, int]


@dataclass(frozen=True)
class BoundingBox:
    normalized: Coordinates
    pixels: Coordinates


@dataclass(frozen=True)
class BlockImage:
    bounding_box_index: int
    filename: str
    data: bytes = field(repr=False)


@dataclass
class OCRBlock:
    label: str
    bounding_boxes: list[BoundingBox]
    content: str
    images: list[BlockImage] = field(default_factory=list)


@dataclass
class OCRDocument:
    width: int
    height: int
    blocks: list[OCRBlock]


# Simplified markers occupy the start of a line; native tokens delimit themselves.
_START = re.compile(r"<\|ref\|>|^[ \t]*[A-Za-z_][A-Za-z0-9_-]*[ \t]*(?=\[\[)", re.MULTILINE)
_NATIVE = re.compile(r"<\|ref\|>([^<>]+)<\|/ref\|>\s*<\|det\|>(.*?)<\|/det\|>", re.DOTALL)
_STRAY_TOKEN = re.compile(r"<\|/?(?:ref|det)\|>")


def _boxes(value: object, width: int, height: int) -> list[BoundingBox]:
    if not isinstance(value, list) or not value:
        raise OCROutputError("Grounding must contain at least one bounding box.")
    result = []
    for box in value:
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(coordinate) is not int or not 0 <= coordinate <= 999 for coordinate in box)
        ):
            raise OCROutputError("Bounding boxes require four integer coordinates in 0–999.")
        x1, y1, x2, y2 = box
        if x1 >= x2 or y1 >= y2:
            raise OCROutputError("Bounding boxes must have increasing x and y coordinates.")
        pixels = (
            int(x1 / 999 * width), int(y1 / 999 * height),
            int(x2 / 999 * width), int(y2 / 999 * height),
        )
        result.append(BoundingBox((x1, y1, x2, y2), pixels))
    return result


def parse_document(text: str, width: int, height: int) -> OCRDocument:
    """Retain following content verbatim; grounding is metadata, never content."""
    blocks: list[OCRBlock] = []
    cursor = 0
    decoder = json.JSONDecoder()
    for start in _START.finditer(text):
        if start.start() < cursor:
            continue
        content = text[cursor:start.start()]
        if _STRAY_TOKEN.search(content):
            raise OCROutputError("Malformed grounding tokens in OCR output.")
        if blocks:
            blocks[-1].content = content
        elif content:
            blocks.append(OCRBlock("text", [], content))

        try:
            if start.group() == "<|ref|>":
                marker = _NATIVE.match(text, start.start())
                if marker is None or not marker[1].strip():
                    raise OCROutputError("Malformed native grounding marker.")
                label = marker[1].strip()
                value = json.loads(marker[2])
                cursor = marker.end()
            else:
                label = start.group().strip()
                value, length = decoder.raw_decode(text[start.end():])
                cursor = start.end() + length
        except (ValueError, RecursionError) as exc:
            raise OCROutputError("Malformed bounding-box coordinates in OCR output.") from exc
        blocks.append(OCRBlock(label, _boxes(value, width, height), ""))

    content = text[cursor:]
    if _STRAY_TOKEN.search(content):
        raise OCROutputError("Malformed grounding tokens in OCR output.")
    if blocks:
        blocks[-1].content = content
    elif content:
        blocks.append(OCRBlock("text", [], content))
    return OCRDocument(width, height, blocks)


def build_document(text: str, image_bytes: bytes) -> OCRDocument:
    """Decode the prepared inference image once, then encode each image crop."""
    with Image.open(BytesIO(image_bytes)) as image:
        document = parse_document(text, *image.size)
        image_index = 0
        for block in document.blocks:
            if block.label != "image":
                continue
            for box_index, box in enumerate(block.bounding_boxes):
                x1, y1, x2, y2 = box.pixels
                if x1 >= x2 or y1 >= y2:
                    raise OCROutputError("Image bounding box produces an empty pixel crop.")
                with image.crop(box.pixels) as crop, BytesIO() as output:
                    crop.save(output, format="PNG")
                    image_index += 1
                    block.images.append(BlockImage(box_index, f"image-{image_index:03d}.png", output.getvalue()))
        return document
