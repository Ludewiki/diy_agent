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
from pydantic import BaseModel

from logging_config import configure_logging
from weather_window import run_prompt
from .callbacks import ProgressCallback, RunCancelled
from .config import Settings
from .database import Database
from .store import (
    WorkerLeaseLost,
    claim_next_run,
    complete_run,
    fail_run,
    get_run_prompt,
    renew_lease,
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
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{run_id}",
            daemon=True,
        )

    def _run(self) -> None:
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
    runner: Runner = run_prompt,
) -> None:
    prompt = get_run_prompt(database, run_id)
    if prompt is None:
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
    callback = ProgressCallback(database, run_id, worker_id)
    try:
        with LeaseHeartbeat(
            database,
            run_id,
            worker_id,
            lease_seconds,
            heartbeat_seconds,
        ) as heartbeat:
            answer = runner(prompt, callbacks=[callback])
            if heartbeat.lost.is_set():
                raise WorkerLeaseLost(f"Worker 已失去 Run {run_id} 的租约。")
        complete_run(database, run_id, worker_id, normalize_answer(answer))
    except RunCancelled as exc:
        record_failure_if_owned(
            database,
            run_id,
            worker_id,
            "RUN_CANCELLED",
            str(exc),
            retryable=False,
            retry_delay_seconds=retry_delay_seconds,
        )
    except WorkerLeaseLost:
        logger.warning(
            "worker lease lost; stale result discarded",
            extra={"event": "worker_lease_lost", "request_id": str(run_id)},
        )
    except Exception:
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


def run_once(
    database: Database,
    runner: Runner = run_prompt,
    *,
    worker_id: str | None = None,
    lease_seconds: float = 120.0,
    heartbeat_seconds: float = 30.0,
    retry_delay_seconds: float = 5.0,
) -> uuid.UUID | None:
    resolved_worker_id = worker_id or create_worker_id()
    run_id = claim_next_run(database, resolved_worker_id, lease_seconds)
    if run_id is None:
        return None
    execute_run(
        database,
        run_id,
        resolved_worker_id,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        retry_delay_seconds=retry_delay_seconds,
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
    while not stopping:
        claimed = run_once(
            database,
            runner,
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
            heartbeat_seconds=settings.worker_heartbeat_seconds,
            retry_delay_seconds=settings.worker_retry_delay_seconds,
        )
        if claimed is None:
            time.sleep(settings.worker_poll_seconds)
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
    database.check_connection()
    if args.once:
        run_once(
            database,
            worker_id=create_worker_id(),
            lease_seconds=settings.worker_lease_seconds,
            heartbeat_seconds=settings.worker_heartbeat_seconds,
            retry_delay_seconds=settings.worker_retry_delay_seconds,
        )
    else:
        serve(database, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
