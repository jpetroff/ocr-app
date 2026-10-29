"""Pin adapter settings so local config.yaml never chooses a test backend."""

from unittest.mock import patch

from app.config import OCRSettings


def select_provider(test, settings=None):
    settings = settings or OCRSettings("http://ollama.test", "deepseek-ocr", 45)
    for entry in ("app.__main__.load_settings", "app.api.load_settings"):
        test.enterContext(patch(entry, return_value=settings))
    return settings
