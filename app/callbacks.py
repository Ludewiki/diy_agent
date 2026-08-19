"""LangChain callbacks that turn Agent activity into durable SSE events."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from .database import Database
from .store import append_event, is_cancel_requested


class RunCancelled(RuntimeError):
    pass


class ProgressCallback(BaseCallbackHandler):
    """Persist coarse-grained model/tool progress without logging tool payloads."""

    raise_error = True

    def __init__(self, database: Database, agent_run_id: str) -> None:
        self.database = database
        self.agent_run_id = agent_run_id
        self._tool_names: dict[str, str] = {}
        self._thinking_emitted = False

    def _check_cancelled(self) -> None:
        if is_cancel_requested(self.database, self.agent_run_id):
            raise RunCancelled("用户已请求取消任务。")

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        self._check_cancelled()
        if not self._thinking_emitted:
            append_event(
                self.database,
                self.agent_run_id,
                "AGENT_THINKING",
                {"message": "Agent 正在分析请求并决定下一步工具调用。"},
            )
            self._thinking_emitted = True

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
        self._tool_names[str(run_id)] = name
        append_event(
            self.database,
            self.agent_run_id,
            "TOOL_STARTED",
            {"tool_name": name},
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        name = self._tool_names.pop(str(run_id), "unknown_tool")
        append_event(
            self.database,
            self.agent_run_id,
            "TOOL_SUCCEEDED",
            {"tool_name": name},
        )
        self._check_cancelled()

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        name = self._tool_names.pop(str(run_id), "unknown_tool")
        append_event(
            self.database,
            self.agent_run_id,
            "TOOL_FAILED",
            {"tool_name": name, "error_type": type(error).__name__},
        )
