"""Separate-process database-backed Worker for long-running Agent runs."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import socket
import threading
import time
from typing import Any, Callable, Sequence
import uuid

from dotenv import load_dotenv
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel

from logging_config import configure_logging
from weather_window import run_prompt
from .callbacks import (
    ProgressCallback,
    RunCancelled,
    RunInvocationLimitExceeded,
)
from .config import Settings
from .context import ContextPolicy, prepare_session_context
from .database import Database
from .store import (
    WorkerLeaseLost,
    append_event,
    claim_next_run,
    complete_run,
    fail_run,
    get_run_execution_input,
    renew_lease,
)
from .telemetry import (
    extract_trace_context,
    record_context_prepared,
    record_run_invocations,
    record_run_execution,
    runtime as telemetry_runtime,
    set_worker_active,
)

logger = logging.getLogger(__name__)
Runner = Callable[..., Any]


def create_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseHeartbeat:
    def __init__(
        self,
        database: Database,
        run_id: uuid.UUID,
        worker_id: str,
        lease_seconds: float,
        heartbeat_seconds: float,
    ) -> None:
        self.database = database
        self.run_id = run_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._parent_context = otel_context.get_current()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{run_id}",
            daemon=True,
        )

    def _run(self) -> None:
        token = otel_context.attach(self._parent_context)
        try:
            while not self._stop.wait(self.heartbeat_seconds):
                try:
                    renewed = renew_lease(
                        self.database,
                        self.run_id,
                        self.worker_id,
                        self.lease_seconds,
                    )
                except Exception:
                    logger.error(
                        "worker heartbeat failed",
                        extra={"event": "lease_heartbeat_failed", "request_id": str(self.run_id)},
                    )
                    continue
                if not renewed:
                    self.lost.set()
                    return
        finally:
            otel_context.detach(token)

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.heartbeat_seconds))


def normalize_answer(answer: Any) -> dict[str, Any]:
    if isinstance(answer, BaseModel):
        value = answer.model_dump(mode="json")
    elif isinstance(answer, dict):
        value = answer
    else:
        value = {"answer": str(answer), "reference": []}
    value.setdefault("answer", "")
    value.setdefault("reference", [])
    return value


def record_failure_if_owned(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    error_code: str,
    error_message: str,
    *,
    retryable: bool,
    retry_delay_seconds: float,
) -> bool:
    try:
        return fail_run(
            database,
            run_id,
            worker_id,
            error_code,
            error_message,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )
    except WorkerLeaseLost:
        logger.warning(
            "worker lease lost; failure result discarded",
            extra={"event": "worker_lease_lost", "request_id": str(run_id)},
        )
        return False


def execute_run(
    database: Database,
    run_id: uuid.UUID,
    worker_id: str,
    *,
    lease_seconds: float,
    heartbeat_seconds: float,
    retry_delay_seconds: float,
    context_policy: ContextPolicy | None = None,
    max_llm_calls: int = 6,
    max_tool_calls: int = 4,
    runner: Runner = run_prompt,
) -> None:
    execution_input = get_run_execution_input(database, run_id)
    if execution_input is None:
        record_failure_if_owned(
            database,
            run_id,
            worker_id,
            "RUN_INPUT_NOT_FOUND",
            "找不到 Run 对应的用户消息。",
            retryable=False,
            retry_delay_seconds=retry_delay_seconds,
        )
        return
    resolved_context_policy = context_policy or ContextPolicy()
    callback = ProgressCallback(
        database,
        run_id,
        worker_id,
        max_llm_calls=max_llm_calls,
        max_tool_calls=max_tool_calls,
    )
    parent_context = extract_trace_context(execution_input.trace_context)
    started = time.perf_counter()
    outcome = "error"
    with telemetry_runtime.tracer.start_as_current_span(
        "agent.run.execute",
        context=parent_context,
        kind=SpanKind.CONSUMER,
        attributes={
            "agent.run.id": str(run_id),
            "agent.session.id": str(execution_input.session_id),
            "agent.run.attempt": execution_input.attempt_count,
            "worker.id": worker_id,
        },
    ) as span:
        try:
            with LeaseHeartbeat(
                database,
                run_id,
                worker_id,
                lease_seconds,
                heartbeat_seconds,
            ) as heartbeat:
                prepared_context = prepare_session_context(
                    database,
                    execution_input.session_id,
                    execution_input.message_id,
                    execution_input.prompt,
                    policy=resolved_context_policy,
                )
                usage = prepared_context.usage
                span.set_attributes(
                    {
                        "agent.context.estimated_input_tokens": usage.estimated_input_tokens,
                        "agent.context.history_messages": usage.history_messages_used,
                        "agent.context.summary_present": usage.summary_present,
                        "agent.context.summary_updated": usage.summary_updated,
                        "agent.context.over_budget": usage.over_budget,
                    }
                )
                record_context_prepared(
                    estimated_input_tokens=usage.estimated_input_tokens,
                    history_messages=usage.history_messages_used,
                    messages_summarized=usage.messages_summarized,
                    messages_truncated=usage.messages_truncated,
                    summary_updated=usage.summary_updated,
                    over_budget=usage.over_budget,
                )
                append_event(
                    database,
                    run_id,
                    "CONTEXT_PREPARED",
                    usage.event_data(),
                    expected_worker_id=worker_id,
                )
                answer = runner(
                    execution_input.prompt,
                    callbacks=[callback],
                    context=prepared_context,
                )
                if heartbeat.lost.is_set():
                    raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
            complete_run(database, run_id, worker_id, normalize_answer(answer))
            outcome = "success"
        except RunCancelled as exc:
            outcome = "cancelled"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            record_failure_if_owned(
                database,
                run_id,
                worker_id,
                "RUN_CANCELLED",
                str(exc),
                retryable=False,
                retry_delay_seconds=retry_delay_seconds,
            )
        except WorkerLeaseLost as exc:
            outcome = "lease_lost"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            logger.warning(
                "worker lease lost; stale result discarded",
                extra={"event": "worker_lease_lost", "request_id": str(run_id)},
            )
        except RunInvocationLimitExceeded as exc:
            outcome = "limit_exceeded"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            record_failure_if_owned(
                database,
                run_id,
                worker_id,
                "AGENT_INVOCATION_LIMIT_EXCEEDED",
                str(exc),
                retryable=False,
                retry_delay_seconds=retry_delay_seconds,
            )
        except Exception as exc:
            outcome = "error"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            logger.error(
                "agent run failed",
                extra={"event": "run_failed", "request_id": str(run_id), "error_code": "AGENT_EXECUTION_FAILED"},
            )
            record_failure_if_owned(
                database,
                run_id,
                worker_id,
                "AGENT_EXECUTION_FAILED",
                "Agent 执行失败，请使用 run_id 查询服务日志。",
                retryable=True,
                retry_delay_seconds=retry_delay_seconds,
            )
        finally:
            span.set_attribute("agent.run.outcome", outcome)
            record_run_execution(
                time.perf_counter() - started,
                outcome=outcome,
            )
            record_run_invocations(
                llm_calls=callback.llm_call_count,
                tool_calls=callback.tool_call_count,
                outcome=outcome,
            )


def run_once(
    database: Database,
    runner: Runner = run_prompt,
    *,
    worker_id: str | None = None,
    lease_seconds: float = 120.0,
    heartbeat_seconds: float = 30.0,
    retry_delay_seconds: float = 5.0,
    context_policy: ContextPolicy | None = None,
    max_llm_calls: int = 6,
    max_tool_calls: int = 4,
) -> uuid.UUID | None:
    resolved_worker_id = worker_id or create_worker_id()
    with telemetry_runtime.tracer.start_as_current_span(
        "worker.claim",
        kind=SpanKind.CONSUMER,
        attributes={"worker.id": resolved_worker_id},
    ) as span:
        run_id = claim_next_run(database, resolved_worker_id, lease_seconds)
        if run_id is not None:
            span.set_attribute("agent.run.id", str(run_id))
    if run_id is None:
        return None
    execute_run(
        database,
        run_id,
        resolved_worker_id,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        retry_delay_seconds=retry_delay_seconds,
        context_policy=context_policy,
        max_llm_calls=max_llm_calls,
        max_tool_calls=max_tool_calls,
        runner=runner,
    )
    return run_id


def serve(
    database: Database,
    settings: Settings,
    runner: Runner = run_prompt,
) -> None:
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    settings.validate()
    database.check_connection()
    worker_id = create_worker_id()
    logger.info(
        "worker started",
        extra={"event": "worker_started", "request_id": worker_id},
    )
    set_worker_active(True)
    try:
        while not stopping:
            claimed = run_once(
                database,
                runner,
                worker_id=worker_id,
                lease_seconds=settings.worker_lease_seconds,
                heartbeat_seconds=settings.worker_heartbeat_seconds,
                retry_delay_seconds=settings.worker_retry_delay_seconds,
                context_policy=ContextPolicy.from_settings(settings),
                max_llm_calls=settings.agent_max_llm_calls,
                max_tool_calls=settings.agent_max_tool_calls,
            )
            if claimed is None:
                time.sleep(settings.worker_poll_seconds)
    finally:
        set_worker_active(False)
        logger.info("worker stopped", extra={"event": "worker_stopped"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Travel Agent durable Worker")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--once", action="store_true", help="处理至多一个任务后退出")
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    configure_logging()
    settings = Settings.from_env()
    settings.validate()
    database = Database(settings.database_url)
    database.require_postgresql()
    telemetry_runtime.configure(
        settings,
        service_role="worker",
        database=database,
    )
    database.check_connection()
    try:
        if args.once:
            run_once(
                database,
                worker_id=create_worker_id(),
                lease_seconds=settings.worker_lease_seconds,
                heartbeat_seconds=settings.worker_heartbeat_seconds,
                retry_delay_seconds=settings.worker_retry_delay_seconds,
                context_policy=ContextPolicy.from_settings(settings),
                max_llm_calls=settings.agent_max_llm_calls,
                max_tool_calls=settings.agent_max_tool_calls,
            )
        else:
            serve(database, settings)
    finally:
        telemetry_runtime.shutdown()
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
