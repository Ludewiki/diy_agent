"""Process one explicitly expected Run with synthetic LLM/Tool callbacks."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any, Sequence
import uuid

from sqlalchemy import or_, select

from app.config import Settings
from app.database import Database
from app.models import AgentRun, RunStatus, utc_now
from app.telemetry import runtime as telemetry_runtime
from app.worker import run_once


def _eligible_run_ids(database: Database) -> list[uuid.UUID]:
    now = utc_now()
    with database.session_factory() as session:
        return list(
            session.scalars(
                select(AgentRun.id)
                .where(
                    or_(
                        AgentRun.status == RunStatus.PENDING.value,
                        (
                            (AgentRun.status == RunStatus.RUNNING.value)
                            & (AgentRun.lease_expires_at <= now)
                        ),
                    )
                )
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )


def synthetic_runner(prompt: str, *, callbacks: list[Any]) -> dict[str, Any]:
    llm_id = uuid.uuid4()
    for callback in callbacks:
        callback.on_chat_model_start(
            {"kwargs": {"model": "synthetic-probe-model"}},
            [[]],
            run_id=llm_id,
        )
    response = SimpleNamespace(
        llm_output={
            "token_usage": {
                "prompt_tokens": 24,
                "completion_tokens": 12,
                "total_tokens": 36,
            }
        },
        generations=[],
    )
    for callback in callbacks:
        callback.on_llm_end(response, run_id=llm_id)

    tool_id = uuid.uuid4()
    for callback in callbacks:
        callback.on_tool_start(
            {"name": "synthetic_probe_tool"},
            "{}",
            run_id=tool_id,
        )
        callback.on_tool_end({"status": "ok"}, run_id=tool_id)
    return {"answer": f"telemetry-probe:{prompt}", "reference": []}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controlled cross-process telemetry probe"
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-run-id", type=uuid.UUID, required=True)
    parser.add_argument(
        "--otel-endpoint",
        default="http://localhost:4318",
    )
    args = parser.parse_args(argv)

    database = Database(args.database_url)
    eligible = _eligible_run_ids(database)
    if eligible != [args.expected_run_id]:
        database.dispose()
        raise RuntimeError(
            "Probe refuses to run unless the expected Run is the only "
            f"eligible Run; found: {[str(item) for item in eligible]}"
        )
    settings = Settings(
        database_url=args.database_url,
        otel_enabled=True,
        otel_exporter_otlp_endpoint=args.otel_endpoint,
        otel_metric_export_interval_ms=1000,
        otel_trace_sample_ratio=1.0,
        otel_service_name_prefix="diy-agent",
    )
    telemetry_runtime.configure(
        settings,
        service_role="worker",
        database=database,
    )
    try:
        processed = run_once(
            database,
            synthetic_runner,
            worker_id="telemetry-probe-worker",
            lease_seconds=10.0,
            heartbeat_seconds=2.0,
            retry_delay_seconds=0.0,
        )
        if processed != args.expected_run_id:
            raise AssertionError(
                f"Expected {args.expected_run_id}, processed {processed}"
            )
        print(processed)
    finally:
        telemetry_runtime.shutdown()
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
