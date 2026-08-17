"""Application logging configuration; importing this module has no side effects."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit one UTF-8 friendly JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "tool_name", "city", "error_code", "request_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once, at an application entry point."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )
