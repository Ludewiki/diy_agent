from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.database import Database
from app.main import create_app
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
    assert all(output["schema_version"] == "1.0" for output in outputs)
    assert all(output["assistant_answer"] == "ok" for output in outputs)
    assert all(output["sources"][0]["title"] == "source" for output in outputs)
    assert event_count == 6


@pytest.mark.postgres
@pytest.mark.integration
def test_concurrent_messages_receive_unique_plan_revisions(
    postgres_database: Database,
) -> None:
    with postgres_database.session_factory.begin() as session:
        user = User(
            email="postgres-revisions@example.com",
            password_hash="not-used-in-this-test",
        )
        session.add(user)
        session.flush()
        conversation = AgentSession(user_id=user.id, title="revision test")
        session.add(conversation)
        session.flush()
        user_id = user.id
        session_id = conversation.id

    def enqueue(number: int) -> uuid.UUID:
        queued = enqueue_message(
            postgres_database,
            session_id,
            user_id,
            f"concurrent request {number}",
        )
        assert queued is not None
        return queued[1].id

    with ThreadPoolExecutor(max_workers=4) as executor:
        run_ids = list(executor.map(enqueue, range(8)))

    with postgres_database.session_factory() as session:
        outputs = list(
            session.scalars(
                select(AgentRun.output_json).where(AgentRun.id.in_(run_ids))
            )
        )
    revisions = sorted(output["plan_revision"] for output in outputs)
    assert revisions == list(range(1, 9))


@pytest.mark.postgres
@pytest.mark.integration
def test_postgres_session_history_and_lifecycle(
    postgres_database: Database,
) -> None:
    app = create_app(
        database=postgres_database,
        settings=Settings(database_url=postgres_database.url),
        sync_runner=lambda prompt: {"answer": prompt, "reference": []},
    )
    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        registered = client.post(
            "/v1/auth/register",
            json={
                "email": "postgres-history@example.com",
                "password": "correct-horse-battery-staple",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert registered.status_code == 201
        client.headers.update({"X-CSRF-Token": csrf})

        first = client.post("/v1/sessions", json={"title": "first"}).json()
        second = client.post("/v1/sessions", json={"title": "second"}).json()
        queued = client.post(
            f"/v1/sessions/{first['id']}/messages",
            json={"content": "postgres preview"},
        )
        assert queued.status_code == 202

        history = client.get("/v1/sessions?page=1&page_size=1").json()
        assert history["total"] == 2
        assert len(history["items"]) == 1
        first_page_id = history["items"][0]["id"]
        assert first_page_id in {first["id"], second["id"]}

        renamed = client.patch(
            f"/v1/sessions/{first['id']}",
            json={"title": "renamed"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "renamed"

        archived = client.patch(
            f"/v1/sessions/{second['id']}",
            json={"archived": True},
        )
        assert archived.status_code == 200
        assert client.get("/v1/sessions").json()["total"] == 1
        assert (
            client.get("/v1/sessions?include_archived=true").json()["total"]
            == 2
        )

        assert client.delete(f"/v1/sessions/{first['id']}").status_code == 204
        assert client.get(f"/v1/sessions/{first['id']}").status_code == 404
