"""Run with `python -m app ocr FILE` or `python -m app serve [--reload]`."""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile

from .config import ConfigError, load_settings
from .errors import OCRError
from .service import create_pipeline


def positive_int(value: str) -> int:
    try:
        number = int(value)
        if number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None


async def run_ocr(args, settings) -> int:
    overrides = {name: getattr(args, name) for name in settings.pipeline.__dataclass_fields__
                 if getattr(args, name) is not None}
    settings = replace(settings, pipeline=replace(settings.pipeline, **overrides))
    async with create_pipeline(settings) as service:
        with args.file.open("rb") as file:
            job = await service.submit(file, args.file.name)
        try:
            async for event in service.events(job):
                data_line = next((line[6:] for line in event.splitlines() if line.startswith("data: ")), None)
                if data_line is None:
                    continue
                data = json.loads(data_line)
                task = data.get("task")
                if task and task["status"] in {"running", "completed", "failed"}:
                    stats = data["stats"]
                    stages = " ".join(
                        f"{name}={counts.get('running', 0)} running/{counts.get('completed', 0)} done"
                        for name, counts in stats["stages"].items())
                    active = ",".join(f"{t.page or '-'}:{t.stage}" for t in job.plan.tasks.values()
                                       if t.status == "running") or "-"
                    message = f"{task['id']} {task['status']} | {stages} | active {active}"
                    if sys.stderr.isatty():
                        print(f"\r\033[2K{message}", end="", file=sys.stderr, flush=True)
                    else:
                        print(message, file=sys.stderr)
            if sys.stderr.isatty():
                print(file=sys.stderr)
            if job.error:
                raise job.error
            if not job.result or not job.result.directory:
                raise OCRError("OCR did not produce a document.")
            parent = args.output_path.resolve()
            parent.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix=f"{Path(job.result.directory).name}-", dir=parent))
            try:
                shutil.copytree(job.result.directory, directory, dirs_exist_ok=True)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            print(directory)
            return 2 if job.status == "partial" else 0
        except asyncio.CancelledError:
            job.cancel_requested = True
            service.wake.set()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Document OCR CLI and API server")
    commands = parser.add_subparsers(dest="command", required=True)
    ocr = commands.add_parser("ocr", help="Recognize an image or PDF and export Markdown with image crops")
    ocr.add_argument("file", type=Path)
    ocr.add_argument("--output-path", type=Path, default=Path.cwd(), help="Parent directory for a new document folder")
    for option in ("pdf-dpi", "image-workers", "llm-workers", "postprocess-workers"):
        ocr.add_argument(f"--{option}", type=positive_int, default=None,
                         help="Override the corresponding pipeline setting")
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
            return asyncio.run(run_ocr(args, settings))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 1
    except (ConfigError, OSError, OCRError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: OCR processing failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
