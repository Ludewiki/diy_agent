"""HTTP request and response contracts, separate from Tool schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RegisterCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class LoginCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime


class CsrfResponse(BaseModel):
    csrf_token: str


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "SessionUpdate":
        if self.title is None and self.archived is None:
            raise ValueError("至少提供 title 或 archived")
        return self


class SessionListItem(SessionResponse):
    recent_message_preview: str | None = None
    message_count: int = 0
    last_run_id: uuid.UUID | None = None
    last_run_status: str | None = None


class SessionListResponse(BaseModel):
    items: list[SessionListItem]
    total: int
    page: int
    page_size: int


class ResultStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ComponentStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class PlanningRequestSnapshot(BaseModel):
    message: str = Field(default="", max_length=20_000)
    city: str | None = Field(default=None, max_length=80)
    trip_days: int | None = Field(default=None, ge=1, le=30)
    interests: list[
        Annotated[str, Field(min_length=1, max_length=100)]
    ] = Field(default_factory=list, max_length=20)
    budget: str | None = Field(default=None, max_length=300)
    additional_preferences: str | None = Field(default=None, max_length=2_000)


class ResultComponent(BaseModel):
    status: ComponentStatus = ComponentStatus.PENDING
    error_code: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=1_000)
    updated_at: datetime | None = None
    inherited_from_run_id: uuid.UUID | None = None


class ResultComponents(BaseModel):
    weather: ResultComponent = Field(default_factory=ResultComponent)
    guide: ResultComponent = Field(default_factory=ResultComponent)
    route: ResultComponent = Field(default_factory=ResultComponent)


class ResultSource(BaseModel):
    provider: str = Field(max_length=100)
    title: str = Field(max_length=500)
    url: str | None = Field(default=None, max_length=2_000)
    purpose: str = Field(max_length=100)
    fetched_at: datetime


class ResultWarning(BaseModel):
    code: str = Field(max_length=100)
    message: str = Field(max_length=2_000)
    severity: Literal["info", "warning", "error"] = "warning"
    scope: Literal["weather", "guide", "route", "agent"] = "agent"


class ContextUsageSnapshot(BaseModel):
    max_input_tokens: int = 0
    estimated_input_tokens: int = 0
    current_message_tokens: int = 0
    history_tokens: int = 0
    summary_tokens: int = 0
    system_reserved_tokens: int = 0
    tool_reserved_tokens: int = 0
    output_reserved_tokens: int = 0
    history_messages_used: int = 0
    messages_summarized: int = 0
    messages_truncated: int = 0
    summary_present: bool = False
    summary_updated: bool = False
    over_budget: bool = False


class RunResultV1(BaseModel):
    """Durable, versioned product result; SSE only announces changes to it."""

    schema_version: Literal["1.0"] = "1.0"
    result_status: ResultStatus = ResultStatus.PARTIAL
    generated_at: datetime
    plan_revision: int = Field(default=1, ge=1)
    supersedes_run_id: uuid.UUID | None = None
    request: PlanningRequestSnapshot = Field(default_factory=PlanningRequestSnapshot)
    assistant_answer: str = ""
    weather_window: dict[str, Any] | None = None
    itinerary: dict[str, Any] | None = None
    sources: list[ResultSource] = Field(default_factory=list, max_length=100)
    warnings: list[ResultWarning] = Field(default_factory=list, max_length=100)
    components: ResultComponents = Field(default_factory=ResultComponents)
    context_usage: ContextUsageSnapshot | None = None


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    planning_context: PlanningRequestSnapshot | None = None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class RunResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID | None
    status: str
    output: RunResultV1
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
    worker_id: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class QueuedRunResponse(BaseModel):
    message: MessageResponse
    run: RunResponse
    events_url: str


class InvokeRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)


class InvokeResponse(BaseModel):
    answer: str
    reference: list[dict[str, str]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    database: str
