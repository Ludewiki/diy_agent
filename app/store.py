"""Transactional persistence operations shared by API, Worker and SSE."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    AgentRun,
    AgentSession,
    Message,
    MessageRole,
    RunEvent,
    RunStatus,
    utc_now,
)


def decode_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def run_to_dict(run: AgentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "user_message_id": run.user_message_id,
        "assistant_message_id": run.assistant_message_id,
        "status": run.status,
        "output": decode_json(run.output_json),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "cancel_requested": run.cancel_requested,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def append_event_in_session(
    session: Session,
    run_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> RunEvent:
    latest = session.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
    )
    event = RunEvent(
        run_id=run_id,
        sequence=int(latest or 0) + 1,
        event_type=event_type,
        data_json=encode_json(data or {}),
    )
    session.add(event)
    session.flush()
    return event


def append_event(
    database: Database,
    run_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> RunEvent:
    with database.session_factory.begin() as session:
        return append_event_in_session(session, run_id, event_type, data)


def enqueue_message(
    database: Database,
    session_id: str,
    content: str,
) -> tuple[Message, AgentRun] | None:
    with database.session_factory.begin() as session:
        conversation = session.get(AgentSession, session_id)
        if conversation is None:
            return None
        message = Message(
            session_id=session_id,
            role=MessageRole.USER.value,
            content=content,
        )
        session.add(message)
        session.flush()
        run = AgentRun(session_id=session_id, user_message_id=message.id)
        session.add(run)
        session.flush()
        append_event_in_session(
            session,
            run.id,
            "RUN_QUEUED",
            {"status": RunStatus.PENDING.value},
        )
        conversation.updated_at = utc_now()
        return message, run


def claim_next_run(database: Database) -> str | None:
    """Atomically transition the oldest pending run to RUNNING."""
    with database.session_factory.begin() as session:
        run_id = session.scalar(
            select(AgentRun.id)
            .where(
                AgentRun.status == RunStatus.PENDING.value,
                AgentRun.cancel_requested.is_(False),
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(1)
        )
        if run_id is None:
            return None
        claimed_at = utc_now()
        result = session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == RunStatus.PENDING.value,
            )
            .values(
                status=RunStatus.RUNNING.value,
                started_at=claimed_at,
                updated_at=claimed_at,
            )
        )
        if result.rowcount != 1:
            return None
        append_event_in_session(
            session,
            run_id,
            "RUN_STARTED",
            {"status": RunStatus.RUNNING.value},
        )
        return run_id


def get_run_prompt(database: Database, run_id: str) -> str | None:
    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return None
        message = session.get(Message, run.user_message_id)
        return message.content if message is not None else None


def is_cancel_requested(database: Database, run_id: str) -> bool:
    with database.session_factory() as session:
        value = session.scalar(
            select(AgentRun.cancel_requested).where(AgentRun.id == run_id)
        )
        return bool(value)


def complete_run(database: Database, run_id: str, output: dict[str, Any]) -> None:
    with database.session_factory.begin() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        if run.cancel_requested:
            run.status = RunStatus.CANCELLED.value
            run.finished_at = utc_now()
            append_event_in_session(
                session,
                run_id,
                "RUN_CANCELLED",
                {"status": RunStatus.CANCELLED.value},
            )
            return
        assistant = Message(
            session_id=run.session_id,
            role=MessageRole.ASSISTANT.value,
            content=str(output.get("answer", "")),
        )
        session.add(assistant)
        session.flush()
        run.assistant_message_id = assistant.id
        run.output_json = encode_json(output)
        run.status = RunStatus.SUCCEEDED.value
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        append_event_in_session(
            session,
            run_id,
            "RUN_SUCCEEDED",
            {"status": RunStatus.SUCCEEDED.value},
        )


def fail_run(
    database: Database,
    run_id: str,
    error_code: str,
    error_message: str,
) -> None:
    with database.session_factory.begin() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        cancelled = run.cancel_requested
        run.status = RunStatus.CANCELLED.value if cancelled else RunStatus.FAILED.value
        run.error_code = "RUN_CANCELLED" if cancelled else error_code
        run.error_message = "用户已请求取消任务。" if cancelled else error_message
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        append_event_in_session(
            session,
            run_id,
            "RUN_CANCELLED" if cancelled else "RUN_FAILED",
            {
                "status": run.status,
                "error_code": run.error_code,
                "message": run.error_message,
            },
        )


def request_cancellation(database: Database, run_id: str) -> AgentRun | None:
    with database.session_factory.begin() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return None
        if run.status in {
            RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            return run
        run.cancel_requested = True
        run.updated_at = utc_now()
        if run.status == RunStatus.PENDING.value:
            run.status = RunStatus.CANCELLED.value
            run.finished_at = utc_now()
            append_event_in_session(
                session,
                run_id,
                "RUN_CANCELLED",
                {"status": RunStatus.CANCELLED.value},
            )
        else:
            append_event_in_session(
                session,
                run_id,
                "CANCELLATION_REQUESTED",
                {"status": run.status},
            )
        return run


def read_events_after(
    database: Database,
    run_id: str,
    after_sequence: int,
) -> tuple[list[dict[str, Any]], str | None]:
    with database.session_factory() as session:
        run_status = session.scalar(
            select(AgentRun.status).where(AgentRun.id == run_id)
        )
        events = session.scalars(
            select(RunEvent)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.sequence > after_sequence,
            )
            .order_by(RunEvent.sequence)
        ).all()
        return [
            {
                "id": event.id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "data": decode_json(event.data_json) or {},
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ], run_status
