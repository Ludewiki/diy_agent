"""Transactional persistence operations shared by API, Worker and SSE."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
from typing import Any
import uuid

from sqlalchemy import and_, func, or_, select, update
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
from .run_results import (
    apply_context_usage,
    apply_tool_result,
    build_initial_result,
    coerce_run_result,
    complete_result,
    fail_result,
)
from .telemetry import (
    inject_trace_context,
    record_run_claim,
    record_run_retry,
)


class WorkerLeaseLost(RuntimeError):
    """Raised when a stale Worker tries to mutate a reclaimed Run."""


@dataclass(frozen=True)
class RunExecutionInput:
    prompt: str
    session_id: uuid.UUID
    message_id: uuid.UUID
    trace_context: dict[str, Any] | None
    attempt_count: int


def _lease_is_active(lease_expires_at: Any) -> bool:
    if lease_expires_at is None:
        return False
    now = utc_now()
    if getattr(lease_expires_at, "tzinfo", None) is None:
        now = now.replace(tzinfo=None)
    return bool(lease_expires_at > now)


def _elapsed_seconds(started_at: Any, finished_at: Any) -> float:
    if getattr(started_at, "tzinfo", None) is None:
        finished_at = finished_at.replace(tzinfo=None)
    return max(0.0, (finished_at - started_at).total_seconds())


def decode_json(value: dict[str, Any] | str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def run_to_dict(run: AgentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "user_message_id": run.user_message_id,
        "assistant_message_id": run.assistant_message_id,
        "status": run.status,
        "output": coerce_run_result(
            decode_json(run.output_json),
            run_status=run.status,
            generated_at=run.finished_at or run.updated_at or run.created_at,
            error_code=run.error_code,
            error_message=run.error_message,
        ),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "cancel_requested": run.cancel_requested,
        "worker_id": run.worker_id,
        "lease_expires_at": run.lease_expires_at,
        "heartbeat_at": run.heartbeat_at,
        "attempt_count": run.attempt_count,
        "max_attempts": run.max_attempts,
        "next_retry_at": run.next_retry_at,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _locked_run(session: Session, run_id: uuid.UUID) -> AgentRun | None:
    statement = select(AgentRun).where(AgentRun.id == run_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement)


def append_event_in_session(
    session: Session,
    run_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    expected_worker_id: str | None = None,
) -> RunEvent:
    run = _locked_run(session, run_id)
    if run is None:
        raise LookupError(f"Run 不存在：{run_id}")
    if expected_worker_id is not None and (
        run.worker_id != expected_worker_id
        or run.status != RunStatus.RUNNING.value
        or not _lease_is_active(run.lease_expires_at)
    ):
        raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
    latest = session.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
    )
    event = RunEvent(
        run_id=run_id,
        sequence=int(latest or 0) + 1,
        event_type=event_type,
        data_json=data or {},
    )
    session.add(event)
    session.flush()
    return event


def append_event(
    database: Database,
    run_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    expected_worker_id: str | None = None,
) -> RunEvent:
    with database.session_factory.begin() as session:
        return append_event_in_session(
            session,
            run_id,
            event_type,
            data,
            expected_worker_id=expected_worker_id,
        )


def enqueue_message(
    database: Database,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    *,
    max_attempts: int = 3,
    planning_context: dict[str, Any] | None = None,
) -> tuple[Message, AgentRun] | None:
    with database.session_factory.begin() as session:
        conversation_statement = select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.user_id == user_id,
        )
        if database.is_postgresql:
            conversation_statement = conversation_statement.with_for_update()
        conversation = session.scalar(conversation_statement)
        if conversation is None:
            return None
        message = Message(
            session_id=session_id,
            role=MessageRole.USER.value,
            content=content,
        )
        session.add(message)
        session.flush()
        previous_success = session.scalar(
            select(AgentRun)
            .where(
                AgentRun.session_id == session_id,
                AgentRun.status == RunStatus.SUCCEEDED.value,
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
        plan_revision = int(
            session.scalar(
                select(func.count(AgentRun.id)).where(
                    AgentRun.session_id == session_id
                )
            )
            or 0
        ) + 1
        run = AgentRun(
            session_id=session_id,
            user_message_id=message.id,
            max_attempts=max_attempts,
            trace_context_json=inject_trace_context() or None,
            output_json=build_initial_result(
                content,
                planning_context=planning_context,
                plan_revision=plan_revision,
                supersedes_run_id=(
                    previous_success.id if previous_success is not None else None
                ),
                previous_output=(
                    decode_json(previous_success.output_json)
                    if previous_success is not None
                    else None
                ),
            ),
        )
        session.add(run)
        session.flush()
        append_event_in_session(
            session,
            run.id,
            "RUN_QUEUED",
            {"status": RunStatus.PENDING.value, "max_attempts": max_attempts},
        )
        conversation.updated_at = utc_now()
        return message, run


def record_context_snapshot(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    usage: dict[str, Any],
) -> None:
    """Persist context usage and its SSE notification atomically."""
    with database.session_factory.begin() as session:
        run = _locked_run(session, run_id)
        if run is None:
            raise LookupError(f"Run 不存在：{run_id}")
        if (
            run.worker_id != worker_id
            or run.status != RunStatus.RUNNING.value
            or not _lease_is_active(run.lease_expires_at)
        ):
            raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
        run.output_json = apply_context_usage(decode_json(run.output_json), usage)
        run.updated_at = utc_now()
        append_event_in_session(
            session,
            run_id,
            "CONTEXT_PREPARED",
            usage,
            expected_worker_id=worker_id,
        )


def record_tool_snapshot(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    tool_name: str,
    snapshot: dict[str, Any] | None,
    *,
    succeeded: bool,
    event_data: dict[str, Any],
) -> None:
    """Persist an allowlisted Tool artifact and its progress event atomically."""
    with database.session_factory.begin() as session:
        run = _locked_run(session, run_id)
        if run is None:
            raise LookupError(f"Run 不存在：{run_id}")
        if (
            run.worker_id != worker_id
            or run.status != RunStatus.RUNNING.value
            or not _lease_is_active(run.lease_expires_at)
        ):
            raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
        run.output_json = apply_tool_result(
            decode_json(run.output_json),
            tool_name,
            snapshot,
            succeeded=succeeded,
        )
        run.updated_at = utc_now()
        append_event_in_session(
            session,
            run_id,
            "TOOL_SUCCEEDED" if succeeded else "TOOL_FAILED",
            event_data,
            expected_worker_id=worker_id,
        )


def _mark_terminal_expired_runs(session: Session, database: Database) -> None:
    now = utc_now()
    statement = (
        select(AgentRun)
        .where(
            AgentRun.status == RunStatus.RUNNING.value,
            AgentRun.lease_expires_at.is_not(None),
            AgentRun.lease_expires_at <= now,
            or_(
                AgentRun.cancel_requested.is_(True),
                AgentRun.attempt_count >= AgentRun.max_attempts,
            ),
        )
        .limit(20)
    )
    if database.is_postgresql:
        statement = statement.with_for_update(skip_locked=True)
    for run in session.scalars(statement):
        cancelled = run.cancel_requested
        run.status = (
            RunStatus.CANCELLED.value if cancelled else RunStatus.FAILED.value
        )
        run.error_code = "RUN_CANCELLED" if cancelled else "MAX_ATTEMPTS_EXCEEDED"
        run.error_message = (
            "用户已请求取消任务。"
            if cancelled
            else "Worker 租约过期且已达到最大尝试次数。"
        )
        run.output_json = fail_result(
            decode_json(run.output_json),
            run.error_code,
            run.error_message,
            terminal=True,
        )
        run.worker_id = None
        run.lease_expires_at = None
        run.finished_at = now
        run.updated_at = now
        append_event_in_session(
            session,
            run.id,
            "RUN_CANCELLED" if cancelled else "RUN_FAILED",
            {
                "status": run.status,
                "error_code": run.error_code,
                "message": run.error_message,
            },
        )


def claim_next_run(
    database: Database,
    worker_id: str,
    lease_seconds: float,
) -> uuid.UUID | None:
    """Claim one eligible Run with PostgreSQL ``SKIP LOCKED`` semantics."""
    with database.session_factory.begin() as session:
        _mark_terminal_expired_runs(session, database)
        now = utc_now()
        pending_ready = and_(
            AgentRun.status == RunStatus.PENDING.value,
            or_(AgentRun.next_retry_at.is_(None), AgentRun.next_retry_at <= now),
        )
        expired_running = and_(
            AgentRun.status == RunStatus.RUNNING.value,
            AgentRun.lease_expires_at.is_not(None),
            AgentRun.lease_expires_at <= now,
            AgentRun.attempt_count < AgentRun.max_attempts,
        )
        statement = (
            select(AgentRun)
            .where(
                AgentRun.cancel_requested.is_(False),
                or_(pending_ready, expired_running),
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(1)
        )
        if database.is_postgresql:
            statement = statement.with_for_update(skip_locked=True)
        run = session.scalar(statement)
        if run is None:
            return None
        reclaimed = run.status == RunStatus.RUNNING.value
        retrying = run.attempt_count > 0 and not reclaimed
        first_claim = run.attempt_count == 0
        queue_seconds = (
            _elapsed_seconds(run.created_at, now)
            if first_claim
            else None
        )
        run.status = RunStatus.RUNNING.value
        run.worker_id = worker_id
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.next_retry_at = None
        run.attempt_count += 1
        run.started_at = run.started_at or now
        run.finished_at = None
        run.updated_at = now
        event_type = (
            "RUN_RECLAIMED"
            if reclaimed
            else "RUN_RETRY_STARTED"
            if retrying
            else "RUN_STARTED"
        )
        append_event_in_session(
            session,
            run.id,
            event_type,
            {
                "status": RunStatus.RUNNING.value,
                "worker_id": worker_id,
                "attempt": run.attempt_count,
                "lease_expires_at": run.lease_expires_at.isoformat(),
            },
            expected_worker_id=worker_id,
        )
        record_run_claim(
            queue_seconds=queue_seconds,
            claim_type=(
                "reclaimed"
                if reclaimed
                else "retry"
                if retrying
                else "initial"
            ),
        )
        return run.id


def renew_lease(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    lease_seconds: float,
) -> bool:
    now = utc_now()
    with database.session_factory.begin() as session:
        result = session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == RunStatus.RUNNING.value,
                AgentRun.worker_id == worker_id,
                AgentRun.lease_expires_at.is_not(None),
                AgentRun.lease_expires_at > now,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        return result.rowcount == 1


def worker_has_lease(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
) -> bool:
    with database.session_factory() as session:
        owner = session.scalar(
            select(AgentRun.worker_id).where(
                AgentRun.id == run_id,
                AgentRun.status == RunStatus.RUNNING.value,
                AgentRun.lease_expires_at.is_not(None),
                AgentRun.lease_expires_at > utc_now(),
            )
        )
        return owner == worker_id


def get_run_prompt(database: Database, run_id: uuid.UUID) -> str | None:
    execution_input = get_run_execution_input(database, run_id)
    return execution_input.prompt if execution_input is not None else None


def get_run_execution_input(
    database: Database,
    run_id: uuid.UUID,
) -> RunExecutionInput | None:
    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return None
        message = session.get(Message, run.user_message_id)
        if message is None:
            return None
        return RunExecutionInput(
            prompt=message.content,
            session_id=run.session_id,
            message_id=message.id,
            trace_context=decode_json(run.trace_context_json),
            attempt_count=run.attempt_count,
        )


def is_cancel_requested(database: Database, run_id: uuid.UUID) -> bool:
    with database.session_factory() as session:
        value = session.scalar(
            select(AgentRun.cancel_requested).where(AgentRun.id == run_id)
        )
        return bool(value)


def complete_run(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    output: dict[str, Any],
) -> bool:
    with database.session_factory.begin() as session:
        run = _locked_run(session, run_id)
        if run is None:
            return False
        if (
            run.worker_id != worker_id
            or run.status != RunStatus.RUNNING.value
            or not _lease_is_active(run.lease_expires_at)
        ):
            raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
        if run.cancel_requested:
            run.status = RunStatus.CANCELLED.value
            run.error_code = "RUN_CANCELLED"
            run.error_message = "用户已请求取消任务。"
            run.output_json = fail_result(
                decode_json(run.output_json),
                run.error_code,
                run.error_message,
                terminal=True,
            )
            run.finished_at = utc_now()
            run.worker_id = None
            run.lease_expires_at = None
            append_event_in_session(
                session,
                run_id,
                "RUN_CANCELLED",
                {"status": RunStatus.CANCELLED.value},
            )
            return True
        final_output = complete_result(decode_json(run.output_json), output)
        assistant = Message(
            session_id=run.session_id,
            role=MessageRole.ASSISTANT.value,
            content=str(final_output.get("assistant_answer", "")),
        )
        session.add(assistant)
        session.flush()
        conversation = session.get(AgentSession, run.session_id)
        if conversation is not None:
            conversation.updated_at = utc_now()
        run.assistant_message_id = assistant.id
        run.output_json = final_output
        run.status = RunStatus.SUCCEEDED.value
        run.error_code = None
        run.error_message = None
        run.worker_id = None
        run.lease_expires_at = None
        run.heartbeat_at = utc_now()
        run.finished_at = run.heartbeat_at
        run.updated_at = run.finished_at
        append_event_in_session(
            session,
            run_id,
            "RUN_SUCCEEDED",
            {"status": RunStatus.SUCCEEDED.value},
        )
        return True


def fail_run(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    error_code: str,
    error_message: str,
    *,
    retryable: bool,
    retry_delay_seconds: float,
) -> bool:
    with database.session_factory.begin() as session:
        run = _locked_run(session, run_id)
        if run is None:
            return False
        if (
            run.worker_id != worker_id
            or run.status != RunStatus.RUNNING.value
            or not _lease_is_active(run.lease_expires_at)
        ):
            raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
        now = utc_now()
        if run.cancel_requested:
            run.status = RunStatus.CANCELLED.value
            run.error_code = "RUN_CANCELLED"
            run.error_message = "用户已请求取消任务。"
            event_type = "RUN_CANCELLED"
            run.finished_at = now
        elif retryable and run.attempt_count < run.max_attempts:
            run.status = RunStatus.PENDING.value
            run.error_code = error_code
            run.error_message = error_message
            run.next_retry_at = now + timedelta(seconds=retry_delay_seconds)
            event_type = "RUN_RETRY_SCHEDULED"
            record_run_retry(error_code)
        else:
            run.status = RunStatus.FAILED.value
            run.error_code = error_code
            run.error_message = error_message
            run.finished_at = now
            event_type = "RUN_FAILED"
        run.output_json = fail_result(
            decode_json(run.output_json),
            str(run.error_code or error_code),
            str(run.error_message or error_message),
            terminal=event_type in {"RUN_FAILED", "RUN_CANCELLED"},
        )
        run.worker_id = None
        run.lease_expires_at = None
        run.updated_at = now
        append_event_in_session(
            session,
            run_id,
            event_type,
            {
                "status": run.status,
                "error_code": run.error_code,
                "message": run.error_message,
                "attempt": run.attempt_count,
                "next_retry_at": (
                    run.next_retry_at.isoformat() if run.next_retry_at else None
                ),
            },
        )
        return True


def request_cancellation(
    database: Database,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AgentRun | None:
    with database.session_factory.begin() as session:
        statement = (
            select(AgentRun)
            .join(AgentSession, AgentSession.id == AgentRun.session_id)
            .where(
                AgentRun.id == run_id,
                AgentSession.user_id == user_id,
            )
        )
        if database.is_postgresql:
            statement = statement.with_for_update()
        run = session.scalar(statement)
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
            run.error_code = "RUN_CANCELLED"
            run.error_message = "用户已请求取消任务。"
            run.finished_at = utc_now()
            run.output_json = fail_result(
                decode_json(run.output_json),
                "RUN_CANCELLED",
                "用户已请求取消任务。",
                terminal=True,
            )
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
    run_id: uuid.UUID,
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
