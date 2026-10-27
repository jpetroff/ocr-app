"""Load the repository's single configuration file."""

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class ConfigError(ValueError):
    """Configuration could not be read or contains invalid settings."""


@dataclass(frozen=True)
class PipelineSettings:
    pdf_dpi: int = 300
    image_workers: int = 4
    llm_workers: int = 1
    postprocess_workers: int = 4

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"pipeline.{name} must be a positive integer.")


@dataclass(frozen=True)
class OllamaSettings:
    base_url: str
    model: str
    timeout_seconds: float
    pipeline: PipelineSettings = PipelineSettings()


def load_settings(path: Path = CONFIG_PATH) -> OllamaSettings:
    try:
        with path.open(encoding="utf-8") as file:
            document = yaml.safe_load(file)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read configuration {path}: {exc}") from exc

    if not isinstance(document, dict) or not isinstance(document.get("ollama"), dict):
        raise ConfigError("config.yaml must contain an 'ollama' mapping.")
    values = document["ollama"]
    base_url = values.get("base_url")
    model = values.get("model")
    timeout = values.get("timeout_seconds")
    try:
        url = urlsplit(base_url) if isinstance(base_url, str) else None
        valid_url = url is not None and url.scheme in {"http", "https"} and bool(url.hostname)
        if url is not None:
            url.port  # Validate an explicitly supplied port.
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError("ollama.base_url must be a valid HTTP or HTTPS URL.")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("ollama.model must be a non-empty string.")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise ConfigError("ollama.timeout_seconds must be a finite positive number.")
    pipeline = document.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise ConfigError("pipeline must be a mapping.")
    if set(pipeline) - set(PipelineSettings.__dataclass_fields__):
        raise ConfigError("Unknown pipeline setting.")
    return OllamaSettings(base_url=base_url, model=model, timeout_seconds=float(timeout),
                          pipeline=PipelineSettings(**pipeline))
