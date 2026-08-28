"""Measure API and durable Worker behavior against a dedicated PostgreSQL DB."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time
from typing import Any, Sequence
import uuid

from alembic import command
from alembic.config import Config as AlembicConfig
import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
import uvicorn

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import AgentRun, RunStatus
from app.store import (
    WorkerLeaseLost,
    claim_next_run,
    complete_run,
)
from app.worker import run_once


def nearest_rank_percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("percentile must be between 0 and 1")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _milliseconds(value: float) -> float:
    return round(value * 1000.0, 3)


def _seconds_between(started_at: Any, finished_at: Any) -> float:
    if getattr(started_at, "tzinfo", None) is None:
        finished_at = finished_at.replace(tzinfo=None)
    return max(0.0, (finished_at - started_at).total_seconds())


def _validate_database_url(database_url: str) -> None:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("Benchmark requires PostgreSQL")
    if not (parsed.database or "").endswith("_benchmark"):
        raise ValueError("Benchmark database name must end with '_benchmark'")


def _reset_public_schema(database_url: str) -> None:
    database = Database(database_url)
    try:
        with database.engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        database.dispose()


def _upgrade_schema(database_url: str) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(AlembicConfig("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _wait_for_api(base_url: str, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise RuntimeError("Benchmark API did not become ready")


def _start_api(
    database: Database,
    *,
    host: str,
    port: int,
) -> tuple[uvicorn.Server, threading.Thread]:
    settings = Settings(
        database_url=database.url,
        worker_poll_seconds=0.01,
        worker_lease_seconds=2.0,
        worker_heartbeat_seconds=0.5,
        worker_retry_delay_seconds=0.0,
        otel_enabled=False,
    )
    application = create_app(database=database, settings=settings)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        name="benchmark-api",
        daemon=True,
    )
    thread.start()
    _wait_for_api(f"http://{host}:{port}")
    return server, thread


def _enqueue_runs(
    base_url: str,
    *,
    run_count: int,
    concurrency: int,
    scenario: str,
) -> tuple[list[uuid.UUID], dict[str, list[float]], float]:
    latencies: dict[str, list[float]] = defaultdict(list)
    latency_lock = threading.Lock()
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    client = httpx.Client(base_url=base_url, timeout=10.0, limits=limits)

    def create_run(index: int) -> uuid.UUID:
        started = time.perf_counter()
        session_response = client.post(
            "/v1/sessions",
            json={"title": f"benchmark-{scenario}-{index}"},
        )
        session_duration = time.perf_counter() - started
        session_response.raise_for_status()
        session_id = session_response.json()["id"]

        started = time.perf_counter()
        run_response = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": f"{scenario}-prompt-{index}"},
        )
        enqueue_duration = time.perf_counter() - started
        run_response.raise_for_status()
        with latency_lock:
            latencies["POST /v1/sessions"].append(session_duration)
            latencies["POST /v1/sessions/{id}/messages"].append(
                enqueue_duration
            )
        return uuid.UUID(run_response.json()["run"]["id"])

    started = time.perf_counter()
    run_ids: list[uuid.UUID] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(create_run, index) for index in range(run_count)]
        for future in as_completed(futures):
            run_ids.append(future.result())
    elapsed = time.perf_counter() - started
    client.close()
    return run_ids, latencies, elapsed


def _run_workers(
    database: Database,
    run_ids: list[uuid.UUID],
    *,
    worker_count: int,
    synthetic_delay_seconds: float,
) -> tuple[dict[str, Any], Counter[str]]:
    target_ids = set(run_ids)
    processed_ids: set[uuid.UUID] = set()
    processed_lock = threading.Lock()
    execution_counts: Counter[str] = Counter()
    execution_lock = threading.Lock()
    callback_tool_ids: dict[str, uuid.UUID] = {}

    def synthetic_runner(prompt: str, *, callbacks: list[Any]) -> dict[str, Any]:
        with execution_lock:
            execution_counts[prompt] += 1
        tool_id = uuid.uuid4()
        callback_tool_ids[prompt] = tool_id
        for callback in callbacks:
            callback.on_tool_start(
                {"name": "synthetic_planner"},
                "{}",
                run_id=tool_id,
            )
        time.sleep(synthetic_delay_seconds)
        for callback in callbacks:
            callback.on_tool_end(
                {"status": "ok"},
                run_id=tool_id,
            )
        return {"answer": f"completed:{prompt}", "reference": []}

    def worker_loop(number: int) -> None:
        worker_id = f"benchmark-worker-{worker_count}-{number}"
        while True:
            with processed_lock:
                if len(processed_ids) >= len(target_ids):
                    return
            claimed = run_once(
                database,
                synthetic_runner,
                worker_id=worker_id,
                lease_seconds=2.0,
                heartbeat_seconds=0.5,
                retry_delay_seconds=0.0,
            )
            if claimed is None:
                time.sleep(0.001)
                continue
            if claimed in target_ids:
                with processed_lock:
                    processed_ids.add(claimed)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(worker_loop, range(worker_count)))
    elapsed = time.perf_counter() - started

    with database.session_factory() as session:
        runs = list(
            session.scalars(
                select(AgentRun).where(AgentRun.id.in_(target_ids))
            )
        )
    queue_seconds = [
        _seconds_between(run.created_at, run.started_at)
        for run in runs
        if run.started_at is not None
    ]
    execution_seconds = [
        _seconds_between(run.started_at, run.finished_at)
        for run in runs
        if run.started_at is not None and run.finished_at is not None
    ]
    duplicate_prompts = sorted(
        prompt for prompt, count in execution_counts.items() if count != 1
    )
    all_succeeded = all(
        run.status == RunStatus.SUCCEEDED.value for run in runs
    )
    exactly_once = (
        len(runs) == len(target_ids)
        and all(run.attempt_count == 1 for run in runs)
        and not duplicate_prompts
        and len(execution_counts) == len(target_ids)
    )
    return (
        {
            "worker_count": worker_count,
            "runs": len(target_ids),
            "wall_time_seconds": round(elapsed, 6),
            "throughput_runs_per_second": round(len(target_ids) / elapsed, 3),
            "queue_latency_ms": {
                "p50": _milliseconds(
                    nearest_rank_percentile(queue_seconds, 0.50)
                ),
                "p95": _milliseconds(
                    nearest_rank_percentile(queue_seconds, 0.95)
                ),
            },
            "execution_latency_ms": {
                "p50": _milliseconds(
                    nearest_rank_percentile(execution_seconds, 0.50)
                ),
                "p95": _milliseconds(
                    nearest_rank_percentile(execution_seconds, 0.95)
                ),
            },
            "all_succeeded": all_succeeded,
            "exactly_once": exactly_once,
            "duplicate_prompt_count": len(duplicate_prompts),
        },
        execution_counts,
    )


def _verify_crash_recovery(
    database: Database,
    base_url: str,
    *,
    lease_seconds: float = 0.5,
) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        session_response = client.post(
            "/v1/sessions",
            json={"title": "crash-recovery"},
        )
        session_response.raise_for_status()
        session_id = session_response.json()["id"]
        run_response = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": "crash-recovery-prompt"},
        )
        run_response.raise_for_status()
        run_id = uuid.UUID(run_response.json()["run"]["id"])

    crashed_worker = "benchmark-crashed-worker"
    claimed = claim_next_run(
        database,
        crashed_worker,
        lease_seconds=lease_seconds,
    )
    if claimed != run_id:
        raise AssertionError("Crashed Worker did not claim the expected Run")
    time.sleep(lease_seconds + 0.15)

    executions = 0

    def recovery_runner(prompt: str, *, callbacks: list[Any]) -> dict[str, Any]:
        nonlocal executions
        executions += 1
        return {"answer": f"recovered:{prompt}", "reference": []}

    recovered = run_once(
        database,
        recovery_runner,
        worker_id="benchmark-recovery-worker",
        lease_seconds=2.0,
        heartbeat_seconds=0.5,
        retry_delay_seconds=0.0,
    )
    stale_worker_fenced = False
    try:
        complete_run(
            database,
            run_id,
            crashed_worker,
            {"answer": "stale", "reference": []},
        )
    except WorkerLeaseLost:
        stale_worker_fenced = True

    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise AssertionError("Recovered Run is missing")
        return {
            "recovered_by_other_worker": recovered == run_id,
            "final_status": run.status,
            "attempt_count": run.attempt_count,
            "runner_execution_count": executions,
            "stale_worker_fenced": stale_worker_fenced,
            "passed": (
                recovered == run_id
                and run.status == RunStatus.SUCCEEDED.value
                and run.attempt_count == 2
                and executions == 1
                and stale_worker_fenced
            ),
        }


def run_benchmark(
    *,
    database_url: str,
    host: str,
    port: int,
    worker_counts: list[int],
    runs_per_scenario: int,
    http_concurrency: int,
    synthetic_delay_seconds: float,
) -> dict[str, Any]:
    _validate_database_url(database_url)
    _reset_public_schema(database_url)
    _upgrade_schema(database_url)
    database = Database(database_url, pool_size=max(worker_counts) + 5)
    server, server_thread = _start_api(database, host=host, port=port)
    base_url = f"http://{host}:{port}"
    api_latencies: dict[str, list[float]] = defaultdict(list)
    api_elapsed_total = 0.0
    worker_results: list[dict[str, Any]] = []
    all_execution_counts: Counter[str] = Counter()
    try:
        for worker_count in worker_counts:
            run_ids, latencies, api_elapsed = _enqueue_runs(
                base_url,
                run_count=runs_per_scenario,
                concurrency=http_concurrency,
                scenario=f"workers-{worker_count}",
            )
            api_elapsed_total += api_elapsed
            for endpoint, values in latencies.items():
                api_latencies[endpoint].extend(values)
            worker_result, execution_counts = _run_workers(
                database,
                run_ids,
                worker_count=worker_count,
                synthetic_delay_seconds=synthetic_delay_seconds,
            )
            worker_results.append(worker_result)
            all_execution_counts.update(execution_counts)

        crash_recovery = _verify_crash_recovery(database, base_url)
        api_summary = {}
        for endpoint, values in api_latencies.items():
            api_summary[endpoint] = {
                "requests": len(values),
                "p50_ms": _milliseconds(
                    nearest_rank_percentile(values, 0.50)
                ),
                "p95_ms": _milliseconds(
                    nearest_rank_percentile(values, 0.95)
                ),
            }
        total_http_requests = sum(len(values) for values in api_latencies.values())
        report = {
            "schema_version": 1,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "host": "local-machine",
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "cpu_count": os.cpu_count(),
                "database": "PostgreSQL dedicated _benchmark database",
            },
            "configuration": {
                "worker_counts": worker_counts,
                "runs_per_scenario": runs_per_scenario,
                "http_concurrency": http_concurrency,
                "synthetic_runner_delay_ms": _milliseconds(
                    synthetic_delay_seconds
                ),
                "runner": "deterministic local stub; no external LLM or Tool network",
            },
            "api": {
                "total_requests": total_http_requests,
                "wall_time_seconds": round(api_elapsed_total, 6),
                "throughput_requests_per_second": round(
                    total_http_requests / api_elapsed_total,
                    3,
                ),
                "endpoints": api_summary,
            },
            "workers": worker_results,
            "crash_recovery": crash_recovery,
            "invariants": {
                "all_normal_runs_succeeded": all(
                    item["all_succeeded"] for item in worker_results
                ),
                "normal_runs_executed_exactly_once": all(
                    item["exactly_once"] for item in worker_results
                ),
                "no_duplicate_normal_prompts": all(
                    count == 1 for count in all_execution_counts.values()
                ),
                "crashed_run_recovered": crash_recovery["passed"],
            },
            "diagnostics": {
                "duplicate_normal_prompt_count": sum(
                    1
                    for count in all_execution_counts.values()
                    if count != 1
                ),
            },
        }
        if not all(report["invariants"].values()):
            raise AssertionError(
                f"Benchmark invariants failed: {report['invariants']}"
            )
        return report
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        database.dispose()


def _parse_worker_counts(value: str) -> list[int]:
    counts = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    if not counts or any(count < 2 or count > 4 for count in counts):
        raise argparse.ArgumentTypeError(
            "worker counts must be a comma-separated subset of 2,3,4"
        )
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Real PostgreSQL API and durable Worker benchmark"
    )
    parser.add_argument("--database-url", default=os.getenv("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--worker-counts", type=_parse_worker_counts, default=[2, 4])
    parser.add_argument("--runs-per-scenario", type=int, default=100)
    parser.add_argument("--http-concurrency", type=int, default=16)
    parser.add_argument("--synthetic-delay-ms", type=float, default=20.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error(
            "--database-url or BENCHMARK_DATABASE_URL is required"
        )
    if args.runs_per_scenario < 1 or args.http_concurrency < 1:
        parser.error("run count and HTTP concurrency must be positive")

    report = run_benchmark(
        database_url=args.database_url,
        host=args.host,
        port=args.port,
        worker_counts=args.worker_counts,
        runs_per_scenario=args.runs_per_scenario,
        http_concurrency=args.http_concurrency,
        synthetic_delay_seconds=args.synthetic_delay_ms / 1000.0,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
