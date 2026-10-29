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
from .page_selection import PageSelection, parse_page_ranges
from .service import create_pipeline


def positive_int(value: str) -> int:
    try:
        number = int(value)
        if number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None


def page_ranges(value: str):
    try:
        return parse_page_ranges(value)
    except OCRError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


async def run_ocr(args, settings) -> int:
    overrides = {name: getattr(args, name) for name in settings.pipeline.__dataclass_fields__
                 if getattr(args, name) is not None}
    settings = replace(settings, pipeline=replace(settings.pipeline, **overrides))
    async with create_pipeline(settings) as service:
        with args.file.open("rb") as file:
            job = await service.submit(file, args.file.name, save_raw=args.save_raw,
                                       page_selection=PageSelection(include=args.include, exclude=args.exclude))
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
            completed = job.result.directory if job.result and not job.error else None
            raw_files = sorted(job.root.glob("raw-*.txt")) if args.save_raw else []
            if completed or raw_files:
                parent = args.output_path.resolve()
                parent.mkdir(parents=True, exist_ok=True)
                prefix = f"{Path(completed).name}-" if completed else "raw-"
                directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
                try:
                    if completed:
                        shutil.copytree(completed, directory, dirs_exist_ok=True)
                    for raw in raw_files:
                        page = int(raw.stem.removeprefix("raw-"))
                        name = f"page-{page:04d}-raw.txt" if job.plan.is_pdf else "raw.txt"
                        shutil.copyfile(raw, directory / name)
                except BaseException:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise
                print(directory)
            if job.error:
                raise job.error
            if not completed:
                raise OCRError("OCR did not produce a document.")
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
    ocr.add_argument("--output-path", "--output", type=Path, default=Path.cwd(), help="Parent directory for a new document folder")
    ocr.add_argument("--save-raw", action="store_true",
                     help="Save raw LLM responses beside the output, even if post-processing fails")
    selection = ocr.add_mutually_exclusive_group()
    for mode in ("include", "exclude"):
        selection.add_argument(f"--{mode}", type=page_ranges, metavar="RANGES",
                               help=f"{mode.capitalize()} one-based pages: 1-23,45-44,50,79-; "
                                    "use = for leading open ranges, e.g. --" + mode + "=-5,8")
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
