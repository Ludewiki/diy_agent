"""Application logging configuration; importing this module has no side effects."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any


_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|token|authorization)(=|%3d)[^& ]+"
)


def redact_sensitive_text(value: Any) -> str:
    """Remove credentials from messages, exception strings, and request URLs."""
    text = str(value)
    text = _QUERY_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        text,
    )
    for variable in ("DEEPSEEK_API_KEY", "ORS_API_KEY"):
        secret = os.getenv(variable)
        if secret:
            text = text.replace(secret, "[REDACTED]")
            text = text.replace(secret.replace("=", "%3D"), "[REDACTED]")
    return text


class JsonFormatter(logging.Formatter):
    """Emit one UTF-8 friendly JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_text(record.getMessage()),
        }
        for key in ("event", "tool_name", "city", "error_code", "request_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact_sensitive_text(
                self.formatException(record.exc_info)
            )
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
