"""Run with `python -m app ocr FILE` or `python -m app serve [--reload]`."""

import argparse
from pathlib import Path
import sys

from .config import ConfigError, load_settings
from .errors import OCRError
from .export import export_document
from .service import create_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Document OCR CLI and API server")
    commands = parser.add_subparsers(dest="command", required=True)
    ocr = commands.add_parser("ocr", help="Recognize one image and export Markdown with image crops")
    ocr.add_argument("file", type=Path)
    ocr.add_argument("--output-path", type=Path, default=Path.cwd(), help="Parent directory for a new document folder")
    serve = commands.add_parser("serve", help="Start the FastAPI server on 127.0.0.1:8000")
    serve.add_argument("--reload", action="store_true", help="Reload on Python source changes")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        if args.command == "serve":
            import uvicorn

            uvicorn.run(
                "app.api:app",
                host="127.0.0.1",
                port=8000,
                reload=args.reload,
                reload_dirs=[str(Path(__file__).resolve().parent)] if args.reload else None,
            )
        else:
            with args.file.open("rb") as file, create_service(settings) as service:
                result = service.ocr(file, args.file.name)
            directory = export_document(result, args.output_path, args.file.name)
            print(directory)
    except (ConfigError, OSError, OCRError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
