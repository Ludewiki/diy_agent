from __future__ import annotations

from datetime import timedelta
import uuid

import pytest

from app.database import Database
from app.models import AgentRun, AgentSession, RunStatus, User, utc_now
from app.store import (
    WorkerLeaseLost,
    claim_next_run,
    complete_run,
    enqueue_message,
    read_events_after,
    renew_lease,
    request_cancellation,
)


@pytest.fixture
def database(tmp_path) -> Database:
    path = (tmp_path / "lease-test.db").as_posix()
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    yield db
    db.dispose()


def _queued_run(database: Database, *, max_attempts: int = 3) -> uuid.UUID:
    with database.session_factory.begin() as session:
        user = User(
            email=f"lease-{uuid.uuid4()}@example.com",
            password_hash="not-used-in-this-test",
        )
        session.add(user)
        session.flush()
        conversation = AgentSession(user_id=user.id, title="lease test")
        session.add(conversation)
        session.flush()
        session_id = conversation.id
        user_id = user.id
    queued = enqueue_message(
        database,
        session_id,
        user_id,
        "plan a trip",
        max_attempts=max_attempts,
    )
    assert queued is not None
    return queued[1].id


def _run_owner_id(database: Database, run_id: uuid.UUID) -> uuid.UUID:
    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        conversation = session.get(AgentSession, run.session_id)
        assert conversation is not None
        return conversation.user_id


def test_expired_run_is_reclaimed_and_stale_worker_is_fenced(
    database: Database,
) -> None:
    run_id = _queued_run(database)
    assert claim_next_run(database, "worker-a", lease_seconds=60) == run_id

    with database.session_factory.begin() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        run.lease_expires_at = utc_now() - timedelta(seconds=1)

    assert claim_next_run(database, "worker-b", lease_seconds=60) == run_id
    with pytest.raises(WorkerLeaseLost):
        complete_run(
            database,
            run_id,
            "worker-a",
            {"answer": "stale", "reference": []},
        )
    assert complete_run(
        database,
        run_id,
        "worker-b",
        {"answer": "fresh", "reference": []},
    )

    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCEEDED.value
        assert run.output_json["schema_version"] == "1.0"
        assert run.output_json["assistant_answer"] == "fresh"
        assert run.attempt_count == 2

    events, _ = read_events_after(database, run_id, 0)
    assert [event["event_type"] for event in events] == [
        "RUN_QUEUED",
        "RUN_STARTED",
        "RUN_RECLAIMED",
        "RUN_SUCCEEDED",
    ]


def test_expired_run_at_attempt_limit_becomes_failed(database: Database) -> None:
    run_id = _queued_run(database, max_attempts=1)
    assert claim_next_run(database, "worker-a", lease_seconds=60) == run_id
    with database.session_factory.begin() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        run.lease_expires_at = utc_now() - timedelta(seconds=1)

    assert claim_next_run(database, "worker-b", lease_seconds=60) is None
    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED.value
        assert run.error_code == "MAX_ATTEMPTS_EXCEEDED"


def test_running_cancellation_keeps_lease_until_worker_records_it(
    database: Database,
) -> None:
    run_id = _queued_run(database)
    assert claim_next_run(database, "worker-a", lease_seconds=60) == run_id
    cancelled = request_cancellation(database, run_id, _run_owner_id(database, run_id))
    assert cancelled is not None
    assert cancelled.cancel_requested is True
    assert renew_lease(database, run_id, "worker-a", lease_seconds=60)

    assert complete_run(
        database,
        run_id,
        "worker-a",
        {"answer": "must be discarded", "reference": []},
    )
    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.CANCELLED.value
        assert run.output_json["schema_version"] == "1.0"
        assert run.output_json["result_status"] == "failed"
        assert run.output_json["assistant_answer"] == ""
        assert any(
            warning["code"] == "RUN_CANCELLED"
            for warning in run.output_json["warnings"]
        )


def test_cancelled_run_with_crashed_worker_is_finalized_after_lease_expiry(
    database: Database,
) -> None:
    run_id = _queued_run(database, max_attempts=3)
    assert claim_next_run(database, "worker-a", lease_seconds=60) == run_id
    assert (
        request_cancellation(database, run_id, _run_owner_id(database, run_id))
        is not None
    )
    with database.session_factory.begin() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        run.lease_expires_at = utc_now() - timedelta(seconds=1)

    assert claim_next_run(database, "worker-b", lease_seconds=60) is None
    with database.session_factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.CANCELLED.value
        assert run.error_code == "RUN_CANCELLED"
