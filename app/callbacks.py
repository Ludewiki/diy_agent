"""LangChain callbacks for durable SSE progress and OpenTelemetry."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
import uuid

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context, trace
from opentelemetry.trace import Status, StatusCode

from .database import Database
from .store import (
    WorkerLeaseLost,
    append_event,
    is_cancel_requested,
    worker_has_lease,
)
from .telemetry import record_llm_call, record_tool_call, runtime


class RunCancelled(RuntimeError):
    pass


@dataclass
class _SpanState:
    span: trace.Span
    context_token: Any
    started: float
    name: str


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def extract_token_usage(response: Any) -> tuple[int, int, int]:
    """Read token usage from common LangChain/OpenAI response shapes."""
    llm_output = getattr(response, "llm_output", None) or {}
    usage = (
        llm_output.get("token_usage")
        or llm_output.get("usage")
        or llm_output.get("usage_metadata")
        or {}
    )
    if usage:
        input_tokens = _non_negative_int(
            usage.get("input_tokens") or usage.get("prompt_tokens")
        )
        output_tokens = _non_negative_int(
            usage.get("output_tokens") or usage.get("completion_tokens")
        )
        total_tokens = _non_negative_int(usage.get("total_tokens"))
        return (
            input_tokens,
            output_tokens,
            total_tokens or input_tokens + output_tokens,
        )

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    seen_messages: set[int] = set()
    for generation_group in getattr(response, "generations", None) or []:
        for generation in generation_group:
            message = getattr(generation, "message", None)
            if message is None or id(message) in seen_messages:
                continue
            seen_messages.add(id(message))
            metadata = getattr(message, "usage_metadata", None) or {}
            input_tokens += _non_negative_int(metadata.get("input_tokens"))
            output_tokens += _non_negative_int(metadata.get("output_tokens"))
            total_tokens += _non_negative_int(metadata.get("total_tokens"))
    return (
        input_tokens,
        output_tokens,
        total_tokens or input_tokens + output_tokens,
    )


def tool_outcome(output: Any) -> str:
    value = output
    if not isinstance(value, dict) and hasattr(value, "content"):
        value = value.content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return "success"
    if isinstance(value, dict) and str(value.get("status", "")).lower() == "error":
        return "error"
    return "success"


def _model_name(serialized: dict[str, Any], kwargs: dict[str, Any]) -> str:
    invocation = kwargs.get("invocation_params") or {}
    serialized_kwargs = serialized.get("kwargs") or {}
    return str(
        invocation.get("model")
        or invocation.get("model_name")
        or serialized_kwargs.get("model")
        or serialized_kwargs.get("model_name")
        or serialized.get("name")
        or "unknown_model"
    )


class ProgressCallback(BaseCallbackHandler):
    """Persist coarse progress and create LLM/Tool child spans."""

    raise_error = True

    def __init__(
        self,
        database: Database,
        agent_run_id: uuid.UUID,
        worker_id: str,
    ) -> None:
        self.database = database
        self.agent_run_id = agent_run_id
        self.worker_id = worker_id
        self._tools: dict[str, _SpanState] = {}
        self._models: dict[str, _SpanState] = {}
        self._thinking_emitted = False

    def _check_cancelled(self) -> None:
        if is_cancel_requested(self.database, self.agent_run_id):
            raise RunCancelled("用户已请求取消任务。")
        if not worker_has_lease(
            self.database,
            self.agent_run_id,
            self.worker_id,
        ):
            raise WorkerLeaseLost(
                f"Worker 已失去 Run {self.agent_run_id} 的租约。"
            )

    def _append(self, event_type: str, data: dict[str, Any]) -> None:
        append_event(
            self.database,
            self.agent_run_id,
            event_type,
            data,
            expected_worker_id=self.worker_id,
        )

    def _start_span(
        self,
        name: str,
        *,
        attributes: dict[str, Any],
    ) -> _SpanState:
        span = runtime.tracer.start_span(name, attributes=attributes)
        token = context.attach(trace.set_span_in_context(span))
        return _SpanState(
            span=span,
            context_token=token,
            started=time.perf_counter(),
            name=str(attributes.get("operation.name") or "unknown"),
        )

    @staticmethod
    def _finish_span(
        state: _SpanState,
        *,
        error: BaseException | None = None,
    ) -> float:
        duration = time.perf_counter() - state.started
        if error is not None:
            state.span.record_exception(error)
            state.span.set_status(Status(StatusCode.ERROR, str(error)))
        context.detach(state.context_token)
        state.span.end()
        return duration

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        self._check_cancelled()
        model = _model_name(serialized, kwargs)
        state = self._start_span(
            "agent.llm.call",
            attributes={
                "operation.name": model,
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": model,
                "agent.run.id": str(self.agent_run_id),
                "worker.id": self.worker_id,
            },
        )
        self._models[str(run_id)] = state
        try:
            if not self._thinking_emitted:
                self._append(
                    "AGENT_THINKING",
                    {"message": "Agent 正在分析请求并决定下一步工具调用。"},
                )
                self._thinking_emitted = True
        except BaseException as error:
            self._models.pop(str(run_id), None)
            duration = self._finish_span(state, error=error)
            record_llm_call(
                model=state.name,
                duration_seconds=duration,
                outcome="error",
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
            )
            raise

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        state = self._models.pop(str(run_id), None)
        if state is None:
            return
        input_tokens, output_tokens, total_tokens = extract_token_usage(response)
        state.span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        state.span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        duration = self._finish_span(state)
        record_llm_call(
            model=state.name,
            duration_seconds=duration,
            outcome="success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        state = self._models.pop(str(run_id), None)
        if state is None:
            return
        duration = self._finish_span(state, error=error)
        record_llm_call(
            model=state.name,
            duration_seconds=duration,
            outcome="error",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        self._check_cancelled()
        name = str(serialized.get("name") or "unknown_tool")
        state = self._start_span(
            f"agent.tool.{name}",
            attributes={
                "operation.name": name,
                "tool.name": name,
                "agent.run.id": str(self.agent_run_id),
                "worker.id": self.worker_id,
            },
        )
        self._tools[str(run_id)] = state
        try:
            self._append("TOOL_STARTED", {"tool_name": name})
        except BaseException as error:
            self._tools.pop(str(run_id), None)
            duration = self._finish_span(state, error=error)
            record_tool_call(name, duration, "error")
            raise

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        state = self._tools.pop(str(run_id), None)
        name = state.name if state is not None else "unknown_tool"
        outcome = tool_outcome(output)
        event_type = "TOOL_SUCCEEDED" if outcome == "success" else "TOOL_FAILED"
        try:
            self._append(event_type, {"tool_name": name})
        finally:
            if state is not None:
                error = (
                    RuntimeError("Tool returned status=error")
                    if outcome == "error"
                    else None
                )
                duration = self._finish_span(state, error=error)
                record_tool_call(name, duration, outcome)
        self._check_cancelled()

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        state = self._tools.pop(str(run_id), None)
        name = state.name if state is not None else "unknown_tool"
        try:
            self._append(
                "TOOL_FAILED",
                {"tool_name": name, "error_type": type(error).__name__},
            )
        finally:
            if state is not None:
                duration = self._finish_span(state, error=error)
                record_tool_call(name, duration, "error")
