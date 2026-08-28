"""Environment-backed runtime settings without import-time secret loading."""

from __future__ import annotations

from dataclasses import dataclass
import os


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false、1/0、yes/no 或 on/off")


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
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_metric_export_interval_ms: int = 5000
    otel_trace_sample_ratio: float = 1.0
    otel_service_name_prefix: str = "diy-agent"
    otel_deployment_environment: str = "development"

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
        if self.otel_metric_export_interval_ms < 1000:
            raise ValueError("OTEL_METRIC_EXPORT_INTERVAL_MS 必须至少为 1000")
        if not 0.0 <= self.otel_trace_sample_ratio <= 1.0:
            raise ValueError("OTEL_TRACE_SAMPLE_RATIO 必须介于 0 和 1 之间")
        if self.otel_enabled and not self.otel_exporter_otlp_endpoint:
            raise ValueError("启用 OpenTelemetry 时必须设置 OTEL_EXPORTER_OTLP_ENDPOINT")

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
            otel_enabled=_environment_bool("OTEL_ENABLED", False),
            otel_exporter_otlp_endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "http://localhost:4318",
            ).rstrip("/"),
            otel_metric_export_interval_ms=int(
                os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000")
            ),
            otel_trace_sample_ratio=float(
                os.getenv("OTEL_TRACE_SAMPLE_RATIO", "1.0")
            ),
            otel_service_name_prefix=os.getenv(
                "OTEL_SERVICE_NAME_PREFIX",
                "diy-agent",
            ),
            otel_deployment_environment=os.getenv(
                "OTEL_DEPLOYMENT_ENVIRONMENT",
                "development",
            ),
        )
