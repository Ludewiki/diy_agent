"""HTTP request and response contracts, separate from Tool schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    created_at: datetime


class RunResponse(BaseModel):
    id: str
    session_id: str
    user_message_id: str
    assistant_message_id: str | None
    status: str
    output: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
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
