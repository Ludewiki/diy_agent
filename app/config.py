"""Environment-backed runtime settings without import-time secret loading."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./resources/agent_api.db"
    worker_poll_seconds: float = 1.0
    sse_poll_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://localhost:5173",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        origins = tuple(
            item.strip()
            for item in os.getenv(
                "CORS_ORIGINS",
                "http://localhost:3000,http://localhost:5173",
            ).split(",")
            if item.strip()
        )
        return cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1.0")),
            sse_poll_seconds=float(os.getenv("SSE_POLL_SECONDS", "0.5")),
            sse_heartbeat_seconds=float(os.getenv("SSE_HEARTBEAT_SECONDS", "15.0")),
            cors_origins=origins,
        )
