from __future__ import annotations

import uuid

import pytest

from app.context import ContextPolicy, TokenCounter, prepare_session_context
from app.database import Database
from app.models import AgentSession, Message, MessageRole, User


@pytest.fixture
def database(tmp_path) -> Database:
    path = (tmp_path / "context-test.db").as_posix()
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    yield db
    db.dispose()


def _conversation_with_messages(
    database: Database,
    contents: list[tuple[str, str]],
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    with database.session_factory.begin() as session:
        user = User(
            email=f"context-{uuid.uuid4()}@example.com",
            password_hash="not-used-in-this-test",
        )
        session.add(user)
        session.flush()
        conversation = AgentSession(user_id=user.id, title="多轮旅行规划")
        session.add(conversation)
        session.flush()
        messages = [
            Message(session_id=conversation.id, role=role, content=content)
            for role, content in contents
        ]
        session.add_all(messages)
        session.flush()
        return conversation.id, [message.id for message in messages]


def test_recent_history_is_bounded_and_excludes_current_message(
    database: Database,
) -> None:
    session_id, message_ids = _conversation_with_messages(
        database,
        [
            (MessageRole.USER.value, "我想去上海"),
            (MessageRole.ASSISTANT.value, "计划几天？"),
            (MessageRole.USER.value, "三天，喜欢建筑"),
        ],
    )
    prepared = prepare_session_context(
        database,
        session_id,
        message_ids[-1],
        "三天，喜欢建筑",
        policy=ContextPolicy(recent_message_limit=4),
    )

    assert [(item.role, item.content) for item in prepared.history] == [
        (MessageRole.USER.value, "我想去上海"),
        (MessageRole.ASSISTANT.value, "计划几天？"),
    ]
    assert all(item.content != "三天，喜欢建筑" for item in prepared.history)
    assert prepared.summary is None
    assert prepared.usage.history_messages_used == 2


def test_rolling_summary_advances_once_and_is_reused(
    database: Database,
) -> None:
    contents = [
        (
            MessageRole.USER.value if index % 2 == 0 else MessageRole.ASSISTANT.value,
            f"第 {index + 1} 条历史消息，包含旅行偏好和约束。",
        )
        for index in range(8)
    ]
    contents.append((MessageRole.USER.value, "请继续规划"))
    session_id, message_ids = _conversation_with_messages(database, contents)
    policy = ContextPolicy(
        max_input_tokens=600,
        system_reserved_tokens=80,
        tool_reserved_tokens=80,
        output_reserved_tokens=100,
        recent_message_limit=2,
        summary_max_tokens=100,
        minimum_recent_tokens=32,
    )

    first = prepare_session_context(
        database,
        session_id,
        message_ids[-1],
        "请继续规划",
        policy=policy,
    )
    with database.session_factory() as session:
        conversation = session.get(AgentSession, session_id)
        assert conversation is not None
        first_count = conversation.summary_message_count
        first_cursor = conversation.summary_through_message_id
        assert first_count == 6
        assert first_cursor == message_ids[5]

    second = prepare_session_context(
        database,
        session_id,
        message_ids[-1],
        "请继续规划",
        policy=policy,
    )
    with database.session_factory() as session:
        conversation = session.get(AgentSession, session_id)
        assert conversation is not None
        assert conversation.summary_message_count == first_count
        assert conversation.summary_through_message_id == first_cursor

    assert first.summary
    assert "第 6 条历史消息" in first.summary
    assert first.usage.summary_updated is True
    assert second.summary == first.summary
    assert second.usage.summary_updated is False
    assert [item.content for item in second.history] == [
        "第 7 条历史消息，包含旅行偏好和约束。",
        "第 8 条历史消息，包含旅行偏好和约束。",
    ]


def test_latest_previous_message_is_preserved_under_tight_budget(
    database: Database,
) -> None:
    long_previous = "最后一轮上下文" * 200
    session_id, message_ids = _conversation_with_messages(
        database,
        [
            (MessageRole.USER.value, "更早的偏好"),
            (MessageRole.ASSISTANT.value, long_previous),
            (MessageRole.USER.value, "继续"),
        ],
    )
    policy = ContextPolicy(
        max_input_tokens=256,
        system_reserved_tokens=100,
        tool_reserved_tokens=100,
        output_reserved_tokens=64,
        recent_message_limit=2,
        summary_max_tokens=32,
        minimum_recent_tokens=32,
    )
    prepared = prepare_session_context(
        database,
        session_id,
        message_ids[-1],
        "继续",
        policy=policy,
    )

    assert prepared.history[-1].role == MessageRole.ASSISTANT.value
    assert prepared.history[-1].content.startswith("最后一轮上下文")
    assert "已按上下文预算截断" in prepared.history[-1].content
    assert prepared.usage.messages_truncated == 1
    assert TokenCounter().count(prepared.history[-1].content) < TokenCounter().count(
        long_previous
    )
