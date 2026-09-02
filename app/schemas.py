"""HTTP request and response contracts, separate from Tool schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RegisterCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)


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


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


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
    output: dict[str, Any] | None
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
