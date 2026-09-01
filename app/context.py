"""Token-budgeted multi-turn context assembly and rolling short-term memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import uuid

from sqlalchemy import select

from .config import Settings
from .database import Database
from .models import AgentSession, Message, MessageRole, utc_now

try:
    import tiktoken
except ImportError:  # pragma: no cover - production dependency has tiktoken
    tiktoken = None


@dataclass(frozen=True)
class ContextPolicy:
    max_input_tokens: int = 12000
    system_reserved_tokens: int = 1400
    tool_reserved_tokens: int = 3200
    output_reserved_tokens: int = 1800
    recent_message_limit: int = 8
    summary_max_tokens: int = 1200
    minimum_recent_tokens: int = 128

    @classmethod
    def from_settings(cls, settings: Settings) -> "ContextPolicy":
        return cls(
            max_input_tokens=settings.context_max_input_tokens,
            system_reserved_tokens=settings.context_system_reserved_tokens,
            tool_reserved_tokens=settings.context_tool_reserved_tokens,
            output_reserved_tokens=settings.context_output_reserved_tokens,
            recent_message_limit=settings.context_recent_message_limit,
            summary_max_tokens=settings.context_summary_max_tokens,
        )


@dataclass(frozen=True)
class ContextMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ContextUsage:
    max_input_tokens: int
    estimated_input_tokens: int
    current_message_tokens: int
    history_tokens: int
    summary_tokens: int
    system_reserved_tokens: int
    tool_reserved_tokens: int
    output_reserved_tokens: int
    history_messages_used: int
    messages_summarized: int
    messages_truncated: int
    summary_present: bool
    summary_updated: bool
    over_budget: bool

    def event_data(self) -> dict[str, int | bool]:
        return {
            "max_input_tokens": self.max_input_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "current_message_tokens": self.current_message_tokens,
            "history_tokens": self.history_tokens,
            "summary_tokens": self.summary_tokens,
            "system_reserved_tokens": self.system_reserved_tokens,
            "tool_reserved_tokens": self.tool_reserved_tokens,
            "output_reserved_tokens": self.output_reserved_tokens,
            "history_messages_used": self.history_messages_used,
            "messages_summarized": self.messages_summarized,
            "messages_truncated": self.messages_truncated,
            "summary_present": self.summary_present,
            "summary_updated": self.summary_updated,
            "over_budget": self.over_budget,
        }


@dataclass(frozen=True)
class PreparedContext:
    history: tuple[ContextMessage, ...]
    summary: str | None
    usage: ContextUsage


class TokenCounter:
    """Stable estimator used for budgeting; model-side usage remains authoritative."""

    def __init__(self) -> None:
        self._encoding = (
            tiktoken.get_encoding("cl100k_base") if tiktoken is not None else None
        )

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        # Conservative fallback: CJK characters are close to one token while
        # Latin text averages roughly four characters per token.
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        return cjk + max(1, (len(text) - cjk + 3) // 4)

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        if self.count(text) <= max_tokens:
            return text
        suffix = "\n…[已按上下文预算截断]"
        suffix_tokens = self.count(suffix)
        body_budget = max(1, max_tokens - suffix_tokens)
        if self._encoding is not None:
            tokens = self._encoding.encode(text)[:body_budget]
            return self._encoding.decode(tokens) + suffix
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count(text[:middle]) <= body_budget:
                low = middle
            else:
                high = middle - 1
        return text[:low] + suffix


class Summarizer(Protocol):
    def summarize(
        self,
        previous_summary: str | None,
        messages: list[Message],
        *,
        max_tokens: int,
        counter: TokenCounter,
    ) -> str:
        ...


class ExtractiveSummarizer:
    """Deterministic, no-extra-LLM rolling summary for reliable Worker execution."""

    _ROLE_LABELS = {
        MessageRole.USER.value: "用户",
        MessageRole.ASSISTANT.value: "助手",
        MessageRole.SYSTEM.value: "系统",
    }

    def summarize(
        self,
        previous_summary: str | None,
        messages: list[Message],
        *,
        max_tokens: int,
        counter: TokenCounter,
    ) -> str:
        if not messages:
            return counter.truncate(previous_summary or "", max_tokens)
        previous_budget = max_tokens // 2 if previous_summary else 0
        parts: list[str] = []
        if previous_summary:
            parts.append(
                "既有摘要:\n"
                + counter.truncate(previous_summary, max(16, previous_budget))
            )
        remaining_budget = max(
            16,
            max_tokens - counter.count("\n".join(parts)),
        )
        # Newest compressed messages appear first so truncation cannot discard
        # the most recent facts while the persisted summary cursor advances.
        new_lines: list[str] = []
        for message in reversed(messages):
            label = self._ROLE_LABELS.get(message.role, message.role)
            normalized = " ".join(message.content.split())
            new_lines.append(f"{label}: {normalized}")
        parts.append(
            "新增历史（由新到旧）:\n"
            + counter.truncate("\n".join(new_lines), remaining_budget)
        )
        return counter.truncate("\n".join(parts), max_tokens)


def _lock_session(database: Database, db_session: object, session_id: uuid.UUID):
    statement = select(AgentSession).where(AgentSession.id == session_id)
    if database.is_postgresql:
        statement = statement.with_for_update()
    return db_session.scalar(statement)


def prepare_session_context(
    database: Database,
    session_id: uuid.UUID,
    current_message_id: uuid.UUID,
    current_prompt: str,
    *,
    policy: ContextPolicy,
    counter: TokenCounter | None = None,
    summarizer: Summarizer | None = None,
) -> PreparedContext:
    """Build bounded history and atomically advance the session summary cursor."""
    token_counter = counter or TokenCounter()
    resolved_summarizer = summarizer or ExtractiveSummarizer()
    with database.session_factory.begin() as db_session:
        conversation = _lock_session(database, db_session, session_id)
        if conversation is None:
            raise LookupError(f"Session 不存在：{session_id}")
        messages = list(
            db_session.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.created_at, Message.id)
            )
        )
        current_index = next(
            (index for index, message in enumerate(messages) if message.id == current_message_id),
            None,
        )
        if current_index is None:
            raise LookupError(f"当前消息不存在：{current_message_id}")
        previous_messages = messages[:current_index]

        summarized_index = -1
        if conversation.summary_through_message_id is not None:
            summarized_index = next(
                (
                    index
                    for index, message in enumerate(previous_messages)
                    if message.id == conversation.summary_through_message_id
                ),
                -1,
            )
        unsummarized = previous_messages[summarized_index + 1 :]
        current_tokens = token_counter.count(current_prompt)
        raw_history_budget = (
            policy.max_input_tokens
            - policy.system_reserved_tokens
            - policy.tool_reserved_tokens
            - current_tokens
        )
        history_budget = max(policy.minimum_recent_tokens, raw_history_budget)

        needs_summary_space = bool(conversation.summary_text) or (
            len(unsummarized) > policy.recent_message_limit
        ) or sum(token_counter.count(message.content) for message in unsummarized) > history_budget
        summary_budget = (
            min(policy.summary_max_tokens, max(64, history_budget // 3))
            if needs_summary_space
            else 0
        )
        recent_budget = max(
            policy.minimum_recent_tokens,
            history_budget - summary_budget,
        )

        selected_reversed: list[Message] = []
        selected_tokens = 0
        for message in reversed(unsummarized):
            if len(selected_reversed) >= policy.recent_message_limit:
                break
            message_tokens = token_counter.count(message.content)
            if selected_reversed and selected_tokens + message_tokens > recent_budget:
                break
            selected_reversed.append(message)
            selected_tokens += message_tokens
            if selected_tokens >= recent_budget:
                break
        selected = list(reversed(selected_reversed))
        dropped_count = len(unsummarized) - len(selected)
        dropped = unsummarized[:dropped_count]

        history: list[ContextMessage] = []
        remaining = recent_budget
        messages_truncated = 0
        for index, message in enumerate(selected):
            content = message.content
            message_tokens = token_counter.count(content)
            is_latest = index == len(selected) - 1
            allowed = max(0, remaining)
            if message_tokens > allowed:
                if not is_latest:
                    continue
                allowed = max(policy.minimum_recent_tokens, allowed)
                content = token_counter.truncate(content, allowed)
                message_tokens = token_counter.count(content)
                messages_truncated += 1
            history.append(ContextMessage(role=message.role, content=content))
            remaining -= message_tokens

        summary = conversation.summary_text
        summary_updated = bool(dropped)
        if dropped:
            summary = resolved_summarizer.summarize(
                conversation.summary_text,
                dropped,
                max_tokens=max(64, summary_budget),
                counter=token_counter,
            )
            conversation.summary_text = summary
            conversation.summary_through_message_id = dropped[-1].id
            conversation.summary_message_count = (
                conversation.summary_message_count or 0
            ) + len(dropped)
            conversation.summary_token_count = token_counter.count(summary)
            conversation.summary_updated_at = utc_now()

        summary_tokens = token_counter.count(summary or "")
        history_tokens = sum(token_counter.count(message.content) for message in history)
        estimated_input = (
            policy.system_reserved_tokens
            + policy.tool_reserved_tokens
            + current_tokens
            + summary_tokens
            + history_tokens
        )
        usage = ContextUsage(
            max_input_tokens=policy.max_input_tokens,
            estimated_input_tokens=estimated_input,
            current_message_tokens=current_tokens,
            history_tokens=history_tokens,
            summary_tokens=summary_tokens,
            system_reserved_tokens=policy.system_reserved_tokens,
            tool_reserved_tokens=policy.tool_reserved_tokens,
            output_reserved_tokens=policy.output_reserved_tokens,
            history_messages_used=len(history),
            messages_summarized=len(dropped),
            messages_truncated=messages_truncated,
            summary_present=bool(summary),
            summary_updated=summary_updated,
            over_budget=estimated_input > policy.max_input_tokens,
        )
        return PreparedContext(history=tuple(history), summary=summary, usage=usage)
