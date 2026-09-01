"""OpenTelemetry setup and low-cardinality business telemetry helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from typing import Any
import uuid

from opentelemetry import context, metrics, propagate, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import (
    ExplicitBucketHistogramAggregation,
    View,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from sqlalchemy import func, select

from .config import Settings
from .database import Database
from .models import AgentRun, RunStatus


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Instruments:
    http_duration: Any
    run_queue_duration: Any
    run_execution_duration: Any
    tool_duration: Any
    tool_calls: Any
    llm_duration: Any
    llm_calls: Any
    llm_tokens: Any
    run_retries: Any
    lease_reclaims: Any
    run_claims: Any
    active_workers: Any
    context_input_tokens: Any
    context_history_messages: Any
    context_messages_summarized: Any
    context_messages_truncated: Any
    context_summaries: Any
    run_llm_invocations: Any
    run_tool_invocations: Any


class TelemetryRuntime:
    """One process-wide OTel SDK runtime.

    The global providers may only be installed once. API and Worker run in
    separate processes, so each process owns one runtime and one service name.
    """

    def __init__(self) -> None:
        self.enabled = False
        self.tracer = trace.get_tracer("diy-agent")
        self.instruments: Instruments | None = None
        self.tracer_provider: TracerProvider | None = None
        self.meter_provider: MeterProvider | None = None
        self._database_engine_ids: set[int] = set()
        self._fastapi_app_ids: set[int] = set()
        self._pending_gauge_database_ids: set[int] = set()
        self._external_clients_instrumented = False

    def configure(
        self,
        settings: Settings,
        *,
        service_role: str,
        database: Database | None = None,
    ) -> None:
        if not settings.otel_enabled:
            return
        if self.enabled:
            if database is not None:
                self.instrument_database(database)
            return

        service_name = f"{settings.otel_service_name_prefix}-{service_role}"
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.6.0",
                "service.instance.id": str(uuid.uuid4()),
                "deployment.environment.name": (
                    settings.otel_deployment_environment
                ),
            }
        )
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(
                TraceIdRatioBased(settings.otel_trace_sample_ratio)
            ),
        )
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
            )
        )

        duration_boundaries = (
            0.001,
            0.0025,
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.5,
            5.0,
            10.0,
            30.0,
            60.0,
            120.0,
        )
        views = [
            View(
                instrument_name=name,
                aggregation=ExplicitBucketHistogramAggregation(
                    duration_boundaries
                ),
            )
            for name in (
                "agent.http.server.duration",
                "agent.run.queue.duration",
                "agent.run.execution.duration",
                "agent.tool.duration",
                "agent.llm.duration",
            )
        ]
        views.append(
            View(
                instrument_name="agent.context.input.tokens",
                aggregation=ExplicitBucketHistogramAggregation(
                    (128, 256, 512, 1024, 2048, 4096, 8192, 12000, 16000)
                ),
            )
        )
        views.extend(
            [
                View(
                    instrument_name=name,
                    aggregation=ExplicitBucketHistogramAggregation(
                        (0, 1, 2, 4, 6, 8, 12, 20, 40)
                    ),
                )
                for name in (
                    "agent.context.history.messages",
                    "agent.run.llm.invocations",
                    "agent.run.tool.invocations",
                )
            ]
        )
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
            export_interval_millis=settings.otel_metric_export_interval_ms,
            export_timeout_millis=min(
                10000,
                settings.otel_metric_export_interval_ms,
            ),
        )
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[metric_reader],
            views=views,
        )

        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        self.tracer_provider = tracer_provider
        self.meter_provider = meter_provider
        self.tracer = trace.get_tracer("diy-agent.runtime", "0.6.0")
        meter = metrics.get_meter("diy-agent.runtime", "0.6.0")
        self.instruments = Instruments(
            http_duration=meter.create_histogram(
                "agent.http.server.duration",
                unit="s",
                description="API request duration by templated route.",
            ),
            run_queue_duration=meter.create_histogram(
                "agent.run.queue.duration",
                unit="s",
                description="Time from Run creation until a Worker first claims it.",
            ),
            run_execution_duration=meter.create_histogram(
                "agent.run.execution.duration",
                unit="s",
                description="Worker execution duration for one Run attempt.",
            ),
            tool_duration=meter.create_histogram(
                "agent.tool.duration",
                unit="s",
                description="LangChain Tool call duration.",
            ),
            tool_calls=meter.create_counter(
                "agent.tool.calls",
                unit="1",
                description="LangChain Tool calls by outcome.",
            ),
            llm_duration=meter.create_histogram(
                "agent.llm.duration",
                unit="s",
                description="LLM call duration.",
            ),
            llm_calls=meter.create_counter(
                "agent.llm.calls",
                unit="1",
                description="LLM calls by outcome and model.",
            ),
            llm_tokens=meter.create_counter(
                "agent.llm.tokens",
                unit="{token}",
                description="LLM input, output, and total token usage.",
            ),
            run_retries=meter.create_counter(
                "agent.run.retries",
                unit="1",
                description="Run retries scheduled after a failed attempt.",
            ),
            lease_reclaims=meter.create_counter(
                "agent.run.lease_reclaims",
                unit="1",
                description="Expired Worker leases reclaimed by another Worker.",
            ),
            run_claims=meter.create_counter(
                "agent.run.claims",
                unit="1",
                description="Run claims by claim type.",
            ),
            active_workers=meter.create_up_down_counter(
                "agent.workers.active",
                unit="1",
                description="Number of active Worker processes.",
            ),
            context_input_tokens=meter.create_histogram(
                "agent.context.input.tokens",
                unit="{token}",
                description="Estimated input context tokens prepared for a Run.",
            ),
            context_history_messages=meter.create_histogram(
                "agent.context.history.messages",
                unit="{message}",
                description="Full recent messages retained in a Run context.",
            ),
            context_messages_summarized=meter.create_counter(
                "agent.context.messages.summarized",
                unit="{message}",
                description="Messages newly folded into rolling summaries.",
            ),
            context_messages_truncated=meter.create_counter(
                "agent.context.messages.truncated",
                unit="{message}",
                description="Recent messages truncated to fit the context budget.",
            ),
            context_summaries=meter.create_counter(
                "agent.context.summaries",
                unit="1",
                description="Rolling summary updates.",
            ),
            run_llm_invocations=meter.create_histogram(
                "agent.run.llm.invocations",
                unit="{call}",
                description="LLM invocations made by one Run attempt.",
            ),
            run_tool_invocations=meter.create_histogram(
                "agent.run.tool.invocations",
                unit="{call}",
                description="Tool invocations made by one Run attempt.",
            ),
        )
        self.enabled = True
        self._instrument_external_clients()
        if database is not None:
            self.instrument_database(database)

    def _instrument_external_clients(self) -> None:
        if self._external_clients_instrumented:
            return
        RequestsInstrumentor().instrument(
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
        )
        HTTPXClientInstrumentor().instrument(
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
        )
        self._external_clients_instrumented = True

    def instrument_database(self, database: Database) -> None:
        if not self.enabled or id(database.engine) in self._database_engine_ids:
            return
        SQLAlchemyInstrumentor().instrument(
            engine=database.engine,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
        )
        self._database_engine_ids.add(id(database.engine))

    def instrument_fastapi(self, application: Any) -> None:
        if not self.enabled or id(application) in self._fastapi_app_ids:
            return
        FastAPIInstrumentor.instrument_app(
            application,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            excluded_urls="health",
            exclude_spans=["receive", "send"],
        )
        self._fastapi_app_ids.add(id(application))

    def register_pending_runs_gauge(self, database: Database) -> None:
        if (
            not self.enabled
            or self.meter_provider is None
            or id(database) in self._pending_gauge_database_ids
        ):
            return

        def observe_pending_runs(
            _: CallbackOptions,
        ) -> Iterable[Observation]:
            try:
                with database.session_factory() as session:
                    count = session.scalar(
                        select(func.count(AgentRun.id)).where(
                            AgentRun.status == RunStatus.PENDING.value
                        )
                    )
                yield Observation(int(count or 0))
            except Exception:
                logger.warning(
                    "pending Run gauge collection failed",
                    extra={"event": "telemetry_gauge_failed"},
                )

        meter = metrics.get_meter("diy-agent.runtime", "0.6.0")
        meter.create_observable_gauge(
            "agent.runs.pending",
            callbacks=[observe_pending_runs],
            description="Number of Runs waiting for a Worker.",
        )
        self._pending_gauge_database_ids.add(id(database))

    def shutdown(self) -> None:
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


runtime = TelemetryRuntime()


def inject_trace_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, Any] | None) -> context.Context:
    normalized = {
        str(key): str(value)
        for key, value in (carrier or {}).items()
        if value is not None
    }
    return propagate.extract(normalized)


def current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)


def record_http_request(
    duration_seconds: float,
    *,
    method: str,
    route: str,
    status_code: int,
) -> None:
    if runtime.instruments is not None:
        runtime.instruments.http_duration.record(
            duration_seconds,
            {
                "http.request.method": method,
                "http.route": route,
                "http.response.status_code": status_code,
            },
        )


def record_run_claim(
    *,
    queue_seconds: float | None,
    claim_type: str,
) -> None:
    if runtime.instruments is None:
        return
    runtime.instruments.run_claims.add(1, {"agent.claim.type": claim_type})
    if queue_seconds is not None:
        runtime.instruments.run_queue_duration.record(
            max(0.0, queue_seconds),
            {"agent.claim.type": claim_type},
        )
    if claim_type == "reclaimed":
        runtime.instruments.lease_reclaims.add(1)


def record_run_execution(duration_seconds: float, *, outcome: str) -> None:
    if runtime.instruments is not None:
        runtime.instruments.run_execution_duration.record(
            duration_seconds,
            {"agent.run.outcome": outcome},
        )


def record_context_prepared(
    *,
    estimated_input_tokens: int,
    history_messages: int,
    messages_summarized: int,
    messages_truncated: int,
    summary_updated: bool,
    over_budget: bool,
) -> None:
    if runtime.instruments is None:
        return
    attributes = {
        "agent.context.summary_updated": summary_updated,
        "agent.context.over_budget": over_budget,
    }
    runtime.instruments.context_input_tokens.record(
        estimated_input_tokens,
        attributes,
    )
    runtime.instruments.context_history_messages.record(
        history_messages,
        attributes,
    )
    if messages_summarized > 0:
        runtime.instruments.context_messages_summarized.add(
            messages_summarized,
            attributes,
        )
    if messages_truncated > 0:
        runtime.instruments.context_messages_truncated.add(
            messages_truncated,
            attributes,
        )
    if summary_updated:
        runtime.instruments.context_summaries.add(1, attributes)


def record_run_invocations(
    *,
    llm_calls: int,
    tool_calls: int,
    outcome: str,
) -> None:
    if runtime.instruments is None:
        return
    attributes = {"agent.run.outcome": outcome}
    runtime.instruments.run_llm_invocations.record(llm_calls, attributes)
    runtime.instruments.run_tool_invocations.record(tool_calls, attributes)


def record_run_retry(error_code: str) -> None:
    if runtime.instruments is not None:
        runtime.instruments.run_retries.add(
            1,
            {"error.type": error_code},
        )


def record_tool_call(name: str, duration_seconds: float, outcome: str) -> None:
    if runtime.instruments is None:
        return
    attributes = {"tool.name": name, "agent.tool.outcome": outcome}
    runtime.instruments.tool_calls.add(1, attributes)
    runtime.instruments.tool_duration.record(duration_seconds, attributes)


def record_llm_call(
    *,
    model: str,
    duration_seconds: float,
    outcome: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> None:
    if runtime.instruments is None:
        return
    attributes = {"gen_ai.request.model": model, "gen_ai.outcome": outcome}
    runtime.instruments.llm_calls.add(1, attributes)
    runtime.instruments.llm_duration.record(duration_seconds, attributes)
    for token_type, value in (
        ("input", input_tokens),
        ("output", output_tokens),
        ("total", total_tokens),
    ):
        if value > 0:
            runtime.instruments.llm_tokens.add(
                value,
                {
                    "gen_ai.request.model": model,
                    "gen_ai.token.type": token_type,
                },
            )


def set_worker_active(active: bool) -> None:
    if runtime.instruments is not None:
        runtime.instruments.active_workers.add(1 if active else -1)
