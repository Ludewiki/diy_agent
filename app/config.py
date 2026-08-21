"""Environment-backed runtime settings without import-time secret loading."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str = (
        "postgresql+psycopg://diy_agent:change-me-for-local-development"
        "@localhost:5432/diy_agent"
    )
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: float = 120.0
    worker_heartbeat_seconds: float = 30.0
    worker_retry_delay_seconds: float = 5.0
    worker_max_attempts: int = 3
    sse_poll_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://localhost:5173",
    )

    def validate(self) -> None:
        if self.worker_poll_seconds <= 0:
            raise ValueError("WORKER_POLL_SECONDS 必须大于 0")
        if self.worker_lease_seconds <= 0:
            raise ValueError("WORKER_LEASE_SECONDS 必须大于 0")
        if self.worker_heartbeat_seconds <= 0:
            raise ValueError("WORKER_HEARTBEAT_SECONDS 必须大于 0")
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS 必须小于 WORKER_LEASE_SECONDS")
        if self.worker_retry_delay_seconds < 0:
            raise ValueError("WORKER_RETRY_DELAY_SECONDS 不能小于 0")
        if self.worker_max_attempts < 1:
            raise ValueError("WORKER_MAX_ATTEMPTS 必须至少为 1")
        if self.sse_poll_seconds <= 0 or self.sse_heartbeat_seconds <= 0:
            raise ValueError("SSE 轮询与心跳时间必须大于 0")

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
            worker_lease_seconds=float(os.getenv("WORKER_LEASE_SECONDS", "120.0")),
            worker_heartbeat_seconds=float(
                os.getenv("WORKER_HEARTBEAT_SECONDS", "30.0")
            ),
            worker_retry_delay_seconds=float(
                os.getenv("WORKER_RETRY_DELAY_SECONDS", "5.0")
            ),
            worker_max_attempts=int(os.getenv("WORKER_MAX_ATTEMPTS", "3")),
            sse_poll_seconds=float(os.getenv("SSE_POLL_SECONDS", "0.5")),
            sse_heartbeat_seconds=float(os.getenv("SSE_HEARTBEAT_SECONDS", "15.0")),
            cors_origins=origins,
        )
