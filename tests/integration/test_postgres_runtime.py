from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.database import Database
from app.models import AgentRun, AgentSession, RunEvent, User
from app.store import claim_next_run, complete_run, enqueue_message


def _reset_public_schema(database: Database) -> None:
    with database.engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def postgres_database() -> Database:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must use PostgreSQL")
    if not (parsed.database or "").endswith("_test"):
        pytest.fail("The PostgreSQL test database name must end with '_test'")

    database = Database(database_url)
    _reset_public_schema(database)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield database
    finally:
        database.dispose()
        cleanup_database = Database(database_url)
        _reset_public_schema(cleanup_database)
        cleanup_database.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.mark.postgres
@pytest.mark.integration
def test_migration_and_concurrent_worker_claims(
    postgres_database: Database,
) -> None:
    run_ids: list[uuid.UUID] = []
    with postgres_database.session_factory.begin() as session:
        user = User(
            email="postgres-runtime@example.com",
            password_hash="not-used-in-this-test",
        )
        session.add(user)
        session.flush()
        user_id = user.id
    for number in range(2):
        with postgres_database.session_factory.begin() as session:
            conversation = AgentSession(
                user_id=user_id,
                title=f"postgres test {number}",
            )
            session.add(conversation)
            session.flush()
            session_id = conversation.id
        queued = enqueue_message(
            postgres_database,
            session_id,
            user_id,
            f"trip request {number}",
        )
        assert queued is not None
        run_ids.append(queued[1].id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(
            executor.map(
                lambda worker: claim_next_run(
                    postgres_database,
                    worker,
                    lease_seconds=60,
                ),
                ("worker-a", "worker-b"),
            )
        )

    assert None not in claimed
    assert set(claimed) == set(run_ids)
    for run_id in claimed:
        assert run_id is not None
        with postgres_database.session_factory() as session:
            worker_id = session.scalar(
                select(AgentRun.worker_id).where(AgentRun.id == run_id)
            )
        assert worker_id is not None
        assert complete_run(
            postgres_database,
            run_id,
            worker_id,
            {"answer": "ok", "reference": [{"title": "source"}]},
        )

    with postgres_database.session_factory() as session:
        outputs = list(session.scalars(select(AgentRun.output_json)))
        event_count = len(list(session.scalars(select(RunEvent.id))))
    assert all(output == {"answer": "ok", "reference": [{"title": "source"}]} for output in outputs)
    assert event_count == 6
