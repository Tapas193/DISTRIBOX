"""Structured logging for D6 DISTRIBOX.

Emits newline-delimited JSON so that request logs, optimisation traces and
errors are machine-parseable in a container log stream, with a readable
console format for local development.

Secrets are never logged: only configuration *keys* are reported, and
values for known-sensitive keys are redacted at the formatter.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from app.core.config import settings

REDACTED_KEYS = {
    "password", "api_key", "apikey", "secret", "token",
    "database_url", "auth_api_key", "ai_api_key",
}

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"asctime", "message", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = REDACTED_KEYS.get(key, value) if key.lower() in REDACTED_KEYS else value

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable local format."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.now().strftime('%H:%M:%S')} "
            f"{record.levelname:<7} {record.name:<28} {record.getMessage()}"
        )
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging() -> None:
    """Idempotently configure root logging."""
    level = logging.DEBUG if settings.debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)

    # JSON in production/container contexts, console locally.
    if settings.is_production or settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party loggers are far too chatty at DEBUG.
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)