from __future__ import annotations

import uuid

import pytest

from app.auth import AuthService
from app.config import Settings
from app.database import Database
from app.demo_seed import LEGACY_USER_ID, seed_demo_user
from app.models import AgentSession, User


@pytest.fixture
def database(tmp_path) -> Database:
    path = (tmp_path / "demo-seed-test.db").as_posix()
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    yield db
    db.dispose()


def test_seed_prefers_163_user_and_reassigns_legacy_sessions(
    database: Database,
) -> None:
    with database.session_factory.begin() as session:
        legacy = User(
            id=LEGACY_USER_ID,
            email="legacy@local.invalid",
            password_hash="disabled",
            is_active=False,
        )
        preferred = User(
            email="demo@163.com",
            password_hash="existing-account",
        )
        session.add_all([legacy, preferred])
        session.flush()
        conversation = AgentSession(user_id=legacy.id, title="legacy trip")
        session.add(conversation)
        session.flush()
        session_id = conversation.id
        preferred_id = preferred.id

    result = seed_demo_user(database, enabled=True)
    assert result.email == "demo@163.com"
    assert result.created is False
    assert result.sessions_reassigned == 1
    with database.session_factory() as session:
        conversation = session.get(AgentSession, session_id)
        assert conversation is not None
        assert conversation.user_id == preferred_id


def test_seed_creates_fallback_admin_only_when_no_163_user(
    database: Database,
) -> None:
    result = seed_demo_user(database, enabled=True)
    assert result.email == "admin@admin.com"
    assert result.created is True

    settings = Settings(database_url=database.url)
    user, _ = AuthService(database, settings).login(
        "admin@admin.com",
        "123456",
    )
    assert user.email == "admin@admin.com"

    repeated = seed_demo_user(database, enabled=True)
    assert repeated.user_id == result.user_id
    assert repeated.created is False


def test_seed_is_inert_without_explicit_local_flag(database: Database) -> None:
    result = seed_demo_user(database, enabled=False)
    assert result.enabled is False
    with database.session_factory() as session:
        assert session.get(User, uuid.uuid4()) is None
