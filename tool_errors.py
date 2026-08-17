"""Shared, JSON-serializable error responses for Agent tools."""

from __future__ import annotations

from typing import Any


def tool_error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    **context: Any,
) -> dict[str, Any]:
    """Build the one recoverable error shape used by every local tool."""
    return {
        "status": "error",
        "error_code": code,
        "message": message,
        "details": details or {},
        **context,
    }
