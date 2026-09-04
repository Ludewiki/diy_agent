from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.memory import DirectLangGraphStoreBackend, MemoryService
from app.models import (
    AgentRun,
    AgentSession,
    MemoryStatus,
    Message,
    MessageRole,
    TravelMemory,
    User,
)
from app.store import enqueue_message
from app.worker import run_once


@pytest.fixture
def database(tmp_path) -> Database:
    path = (tmp_path / "long-term-memory.db").as_posix()
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    yield db
    db.dispose()


@pytest.fixture
def memory_service(database: Database) -> MemoryService:
    return MemoryService(
        database,
        DirectLangGraphStoreBackend(InMemoryStore()),
        recall_limit=8,
    )


def _source_run(database: Database, email: str) -> tuple[User, Message, AgentRun]:
    with database.session_factory.begin() as session:
        user = User(email=email, password_hash="test-only")
        session.add(user)
        session.flush()
        conversation = AgentSession(user_id=user.id, title="source")
        session.add(conversation)
        session.flush()
        message = Message(
            session_id=conversation.id,
            role=MessageRole.USER.value,
            content="source",
        )
        session.add(message)
        session.flush()
        run = AgentRun(
            session_id=conversation.id,
            user_message_id=message.id,
        )
        session.add(run)
        session.flush()
        return user, message, run


def test_extraction_is_governed_idempotent_and_user_scoped(
    database: Database,
    memory_service: MemoryService,
) -> None:
    alice, message, run = _source_run(database, "memory-alice@example.com")
    bob, _, _ = _source_run(database, "memory-bob@example.com")
    request = {
        "interests": ["历史建筑"],
        "budget": "适中预算",
        "additional_preferences": "本次带长辈",
    }

    for _ in range(2):
        memory_service.extract_from_run(
            user_id=alice.id,
            message_id=message.id,
            run_id=run.id,
            prompt="请记住我喜欢历史街区，而且我不喜欢赶行程。",
            planning_request=request,
        )

    with database.session_factory() as session:
        memories = list(
            session.scalars(
                select(TravelMemory).where(TravelMemory.user_id == alice.id)
            )
        )
        count = session.scalar(select(func.count(TravelMemory.id)))
    assert count == len(memories)
    assert len(memories) == 4
    assert sum(item.status == MemoryStatus.CONFIRMED.value for item in memories) == 1
    assert sum(item.status == MemoryStatus.CANDIDATE.value for item in memories) == 3
    recalled = memory_service.recall(alice.id, "规划历史街区慢旅行")
    assert any("历史街区" in item for item in recalled)
    assert memory_service.recall(bob.id, "规划历史街区慢旅行") == []


def test_deleted_memory_is_not_recreated_by_automatic_extraction(
    database: Database,
    memory_service: MemoryService,
) -> None:
    user, message, run = _source_run(database, "memory-delete@example.com")
    created = memory_service.extract_from_run(
        user_id=user.id,
        message_id=message.id,
        run_id=run.id,
        prompt="请记住我喜欢博物馆。",
        planning_request=None,
    )[0]
    assert memory_service.delete(user.id, created.id)

    recreated = memory_service.extract_from_run(
        user_id=user.id,
        message_id=message.id,
        run_id=run.id,
        prompt="请记住我喜欢博物馆。",
        planning_request=None,
    )
    assert recreated == []
    assert memory_service.recall(user.id, "博物馆") == []


def test_worker_recalls_confirmed_memory_across_sessions(
    database: Database,
    memory_service: MemoryService,
) -> None:
    with database.session_factory.begin() as session:
        user = User(email="cross-session@example.com", password_hash="test-only")
        session.add(user)
        session.flush()
        first = AgentSession(user_id=user.id, title="first")
        session.add(first)
        session.flush()
        user_id = user.id
        first_id = first.id

    first_queued = enqueue_message(
        database,
        first_id,
        user_id,
        "请记住我喜欢历史建筑。",
    )
    assert first_queued is not None
    assert run_once(
        database,
        lambda prompt, *, callbacks, context: {
            "answer": "已记住",
            "reference": [],
        },
        memory_service=memory_service,
    ) == first_queued[1].id

    with database.session_factory.begin() as session:
        second = AgentSession(user_id=user_id, title="second")
        session.add(second)
        session.flush()
        second_id = second.id
    second_queued = enqueue_message(
        database,
        second_id,
        user_id,
        "帮我规划东京旅行",
    )
    assert second_queued is not None
    captured = []

    def capture(prompt: str, *, callbacks, context) -> dict:
        captured.append(context)
        return {"answer": "个性化方案", "reference": []}

    assert run_once(
        database,
        capture,
        memory_service=memory_service,
    ) == second_queued[1].id
    assert captured[0].long_term_memory
    assert "历史建筑" in captured[0].long_term_memory
    assert captured[0].usage.long_term_memories_recalled == 1
    assert captured[0].usage.long_term_memory_tokens > 0


def _register(client: TestClient, email: str) -> str:
    csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "12345678"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    client.headers.update({"X-CSRF-Token": csrf})
    return response.json()["id"]


def test_memory_management_api_enforces_user_isolation(
    database: Database,
    memory_service: MemoryService,
) -> None:
    application = create_app(
        database=database,
        settings=Settings(database_url=database.url),
        memory_service=memory_service,
        sync_runner=lambda prompt: {"answer": prompt, "reference": []},
    )
    with TestClient(application) as alice, TestClient(application) as bob:
        _register(alice, "api-memory-alice@example.com")
        _register(bob, "api-memory-bob@example.com")
        created = alice.post(
            "/v1/memories",
            json={
                "content": "我喜欢历史建筑",
                "memory_type": "preference",
            },
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]
        assert created.json()["status"] == "CONFIRMED"

        assert alice.get("/v1/memories").json()["confirmed_count"] == 1
        assert bob.get("/v1/memories").json()["total"] == 0
        assert (
            bob.patch(
                f"/v1/memories/{memory_id}",
                json={"content": "越权修改"},
            ).status_code
            == 404
        )
        assert bob.delete(f"/v1/memories/{memory_id}").status_code == 404

        updated = alice.patch(
            f"/v1/memories/{memory_id}",
            json={"content": "我喜欢历史建筑和博物馆"},
        )
        assert updated.status_code == 200
        assert "博物馆" in updated.json()["content"]
        assert alice.delete(f"/v1/memories/{memory_id}").status_code == 204
        assert alice.get("/v1/memories").json()["total"] == 0
