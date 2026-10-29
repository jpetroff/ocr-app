"""Load the repository's single configuration file."""

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DEFAULT_OCR_PROMPT = "<|grounding|>Convert the document to markdown."


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
class OCRSettings:
    base_url: str
    model: str
    timeout_seconds: float
    pipeline: PipelineSettings = PipelineSettings()
    provider: Literal["ollama", "openailike"] = "ollama"
    prompt: str = DEFAULT_OCR_PROMPT


OllamaSettings = OCRSettings


def load_settings(path: Path = CONFIG_PATH) -> OCRSettings:
    try:
        with path.open(encoding="utf-8") as file:
            document = yaml.safe_load(file)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read configuration {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise ConfigError("config.yaml must contain a mapping.")
    provider = document.get("provider", "ollama")
    if not isinstance(provider, str) or provider not in {"ollama", "openailike"}:
        raise ConfigError("provider must be 'ollama' or 'openailike'.")
    if not isinstance(document.get(provider), dict):
        raise ConfigError(f"config.yaml must contain an '{provider}' mapping.")
    values = document[provider]
    base_url = values.get("base_url")
    model = values.get("model")
    timeout_key = "timeout_seconds" if provider == "ollama" else "timeout"
    timeout = values.get(timeout_key)
    prompt = values.get("prompt", DEFAULT_OCR_PROMPT) if provider == "openailike" else DEFAULT_OCR_PROMPT
    try:
        url = urlsplit(base_url) if isinstance(base_url, str) else None
        valid_url = url is not None and url.scheme in {"http", "https"} and bool(url.hostname)
        if url is not None:
            url.port  # Validate an explicitly supplied port.
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError(f"{provider}.base_url must be a valid HTTP or HTTPS URL.")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{provider}.model must be a non-empty string.")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise ConfigError(f"{provider}.{timeout_key} must be a finite positive number.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError("openailike.prompt must be a non-empty string.")
    pipeline = document.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise ConfigError("pipeline must be a mapping.")
    if set(pipeline) - set(PipelineSettings.__dataclass_fields__):
        raise ConfigError("Unknown pipeline setting.")
    return OCRSettings(base_url=base_url, model=model, timeout_seconds=float(timeout),
                       pipeline=PipelineSettings(**pipeline), provider=provider, prompt=prompt)
