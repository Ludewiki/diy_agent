"""Separate-process database-backed Worker for long-running Agent runs."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import signal
import time
from typing import Any, Callable, Sequence

from dotenv import load_dotenv
from pydantic import BaseModel

from logging_config import configure_logging
from weather_window import run_prompt
from .callbacks import ProgressCallback, RunCancelled
from .config import Settings
from .database import Database
from .store import claim_next_run, complete_run, fail_run, get_run_prompt

logger = logging.getLogger(__name__)
Runner = Callable[..., Any]


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


def execute_run(database: Database, run_id: str, runner: Runner = run_prompt) -> None:
    prompt = get_run_prompt(database, run_id)
    if prompt is None:
        fail_run(database, run_id, "RUN_INPUT_NOT_FOUND", "找不到 Run 对应的用户消息。")
        return
    callback = ProgressCallback(database, run_id)
    try:
        answer = runner(prompt, callbacks=[callback])
        complete_run(database, run_id, normalize_answer(answer))
    except RunCancelled as exc:
        fail_run(database, run_id, "RUN_CANCELLED", str(exc))
    except Exception:
        logger.error(
            "agent run failed",
            extra={"event": "run_failed", "request_id": run_id, "error_code": "AGENT_EXECUTION_FAILED"},
        )
        fail_run(
            database,
            run_id,
            "AGENT_EXECUTION_FAILED",
            "Agent 执行失败，请使用 run_id 查询服务日志。",
        )


def run_once(database: Database, runner: Runner = run_prompt) -> str | None:
    run_id = claim_next_run(database)
    if run_id is None:
        return None
    execute_run(database, run_id, runner)
    return run_id


def serve(database: Database, poll_seconds: float, runner: Runner = run_prompt) -> None:
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    database.create_schema()
    logger.info("worker started", extra={"event": "worker_started"})
    while not stopping:
        claimed = run_once(database, runner)
        if claimed is None:
            time.sleep(poll_seconds)
    logger.info("worker stopped", extra={"event": "worker_stopped"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Travel Agent durable Worker")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--once", action="store_true", help="处理至多一个任务后退出")
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    configure_logging()
    settings = Settings.from_env()
    database = Database(settings.database_url)
    database.create_schema()
    if args.once:
        run_once(database)
    else:
        serve(database, settings.worker_poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
