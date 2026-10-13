"""Keys from .env (rule 9). Values are loaded for use but never printed or logged."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, load_dotenv

KeyState = Literal["filled", "placeholder", "missing"]

# Anything shaped like a common API key or token is masked in logs even if not in .env.
_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot tokens
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),  # Google access tokens
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),  # Google refresh tokens
]
REDACTED = "[REDACTED]"


def load_env(env_file: Path) -> None:
    """Load .env into the process environment without overriding values already set."""
    if env_file.exists():
        load_dotenv(env_file, override=False)


def key_state(env_file: Path, example_file: Path, name: str) -> KeyState:
    """Whether a key in .env is filled in, still the template placeholder, or absent."""
    if not env_file.exists():
        return "missing"
    value = (dotenv_values(env_file).get(name) or "").strip()
    if not value:
        return "missing"
    placeholder = (dotenv_values(example_file).get(name) or "").strip() if example_file.exists() else ""
    if value == placeholder or value.startswith("<") or "paste" in value.lower() or "your" in value.lower():
        return "placeholder"
    return "filled"


def template_has_secret(example_file: Path) -> bool:
    """True if the committed template (.env.example) holds something shaped like a real key."""
    if not example_file.exists():
        return False
    text = example_file.read_text(encoding="utf-8", errors="ignore")
    return any(pattern.search(text) for pattern in _KEY_PATTERNS)


class RedactingFilter(logging.Filter):
    """Masks secret values from .env and key-shaped strings in every log record."""

    def __init__(self, env_file: Path | None = None) -> None:
        super().__init__()
        secrets = []
        if env_file is not None and env_file.exists():
            secrets = [v for v in dotenv_values(env_file).values() if v and len(v) >= 8]
        self._secrets = sorted(set(secrets), key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _KEY_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self.redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def setup_logging(logs_dir: Path, env_file: Path, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    redactor = RedactingFilter(env_file)
    file_handler = logging.FileHandler(logs_dir / "applier.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    file_handler.addFilter(redactor)
    file_handler._applier = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_applier", False):
            root.removeHandler(handler)
            handler.close()
    root.addHandler(file_handler)
    # Libraries that may log request details stay quiet unless something goes wrong.
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
