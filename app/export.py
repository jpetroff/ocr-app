"""Export an OCR document as a unique directory or a disk-backed ZIP."""

from pathlib import Path
import re
import shutil
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from .document import OCRDocument


def render_markdown(document: OCRDocument) -> str:
    parts = []
    for block in document.blocks:
        for image in block.images:
            parts.append(f"![Image]({image.filename})")
        if block.content:
            parts.append(block.content)
    return "\n\n".join(parts)


def export_document(document: OCRDocument, parent: Path, filename: str) -> Path:
    """Create a new child directory; remove it if any write fails."""
    parent = parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    basename = re.split(r"[/\\]", filename)[-1]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(basename).stem).strip("-_")[:80] or "document"
    directory = Path(tempfile.mkdtemp(prefix=f"{stem}-", dir=parent))
    try:
        (directory / "document.md").write_text(render_markdown(document), encoding="utf-8")
        for block in document.blocks:
            for image in block.images:
                (directory / image.filename).write_bytes(image.data)
    except BaseException:
        shutil.rmtree(directory)
        raise
    return directory


def archive_document(directory: Path, archive: Path) -> None:
    """Store a single top-level document directory without buffering the ZIP."""
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
        for file in sorted(directory.iterdir()):
            output.write(file, arcname=f"{directory.name}/{file.name}")
