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
    context_max_input_tokens: int = 12000
    context_system_reserved_tokens: int = 1400
    context_tool_reserved_tokens: int = 3200
    context_output_reserved_tokens: int = 1800
    context_recent_message_limit: int = 8
    context_summary_max_tokens: int = 1200
    agent_max_llm_calls: int = 6
    agent_max_tool_calls: int = 4
    auth_cookie_name: str = "diy_agent_session"
    csrf_cookie_name: str = "diy_agent_csrf"
    auth_session_lifetime_days: int = 7
    auth_cookie_secure: bool = False
    demo_user_enabled: bool = False
    csrf_trusted_origins: tuple[str, ...] = (
        "http://localhost:8000",
        "http://127.0.0.1:8000",
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
        if self.otel_metric_export_interval_ms < 1000:
            raise ValueError("OTEL_METRIC_EXPORT_INTERVAL_MS 必须至少为 1000")
        if not 0.0 <= self.otel_trace_sample_ratio <= 1.0:
            raise ValueError("OTEL_TRACE_SAMPLE_RATIO 必须介于 0 和 1 之间")
        if self.otel_enabled and not self.otel_exporter_otlp_endpoint:
            raise ValueError("启用 OpenTelemetry 时必须设置 OTEL_EXPORTER_OTLP_ENDPOINT")
        if self.context_max_input_tokens < 1024:
            raise ValueError("CONTEXT_MAX_INPUT_TOKENS 必须至少为 1024")
        if min(
            self.context_system_reserved_tokens,
            self.context_tool_reserved_tokens,
            self.context_output_reserved_tokens,
            self.context_summary_max_tokens,
        ) < 0:
            raise ValueError("上下文各项 Token 预算不能小于 0")
        if (
            self.context_system_reserved_tokens + self.context_tool_reserved_tokens
            >= self.context_max_input_tokens
        ):
            raise ValueError("系统提示与 Tool 预留之和必须小于输入上下文预算")
        if self.context_recent_message_limit < 1:
            raise ValueError("CONTEXT_RECENT_MESSAGE_LIMIT 必须至少为 1")
        if self.agent_max_llm_calls < 1 or self.agent_max_tool_calls < 1:
            raise ValueError("Agent 的 LLM 与 Tool 调用上限必须至少为 1")
        if self.auth_session_lifetime_days < 1:
            raise ValueError("AUTH_SESSION_LIFETIME_DAYS 必须至少为 1")
        if not self.auth_cookie_name or not self.csrf_cookie_name:
            raise ValueError("认证 Cookie 名称不能为空")

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
        trusted_origins = tuple(
            item.strip().rstrip("/")
            for item in os.getenv(
                "CSRF_TRUSTED_ORIGINS",
                "http://localhost:8000,http://127.0.0.1:8000",
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
            context_max_input_tokens=int(
                os.getenv("CONTEXT_MAX_INPUT_TOKENS", "12000")
            ),
            context_system_reserved_tokens=int(
                os.getenv("CONTEXT_SYSTEM_RESERVED_TOKENS", "1400")
            ),
            context_tool_reserved_tokens=int(
                os.getenv("CONTEXT_TOOL_RESERVED_TOKENS", "3200")
            ),
            context_output_reserved_tokens=int(
                os.getenv("CONTEXT_OUTPUT_RESERVED_TOKENS", "1800")
            ),
            context_recent_message_limit=int(
                os.getenv("CONTEXT_RECENT_MESSAGE_LIMIT", "8")
            ),
            context_summary_max_tokens=int(
                os.getenv("CONTEXT_SUMMARY_MAX_TOKENS", "1200")
            ),
            agent_max_llm_calls=int(os.getenv("AGENT_MAX_LLM_CALLS", "6")),
            agent_max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "4")),
            auth_cookie_name=os.getenv("AUTH_COOKIE_NAME", "diy_agent_session"),
            csrf_cookie_name=os.getenv("CSRF_COOKIE_NAME", "diy_agent_csrf"),
            auth_session_lifetime_days=int(
                os.getenv("AUTH_SESSION_LIFETIME_DAYS", "7")
            ),
            auth_cookie_secure=_environment_bool("AUTH_COOKIE_SECURE", False),
            demo_user_enabled=_environment_bool("DEMO_USER_ENABLED", False),
            csrf_trusted_origins=trusted_origins,
        )
