from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.worker import run_once


@pytest.fixture
def database(tmp_path) -> Database:
    path = (tmp_path / "agent-test.db").as_posix()
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    yield db
    db.dispose()


@pytest.fixture
def client(database: Database) -> TestClient:
    settings = Settings(
        database_url=database.url,
        sse_poll_seconds=0.01,
        sse_heartbeat_seconds=0.05,
    )
    app = create_app(
        database=database,
        settings=settings,
        sync_runner=lambda prompt: {
            "answer": f"同步回答：{prompt}",
            "reference": [],
        },
    )
    with TestClient(app) as test_client:
        yield test_client


def _create_queued_run(client: TestClient) -> tuple[str, str]:
    session_response = client.post("/v1/sessions", json={"title": "上海旅行"})
    assert session_response.status_code == 201
    session_id = session_response.json()["id"]
    queued_response = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "帮我规划近期去上海三天"},
    )
    assert queued_response.status_code == 202
    return session_id, queued_response.json()["run"]["id"]


def test_synchronous_agent_endpoint(client: TestClient) -> None:
    response = client.post("/v1/agent/invoke", json={"prompt": "规划上海旅行"})
    assert response.status_code == 200
    assert response.json() == {"answer": "同步回答：规划上海旅行", "reference": []}
    assert response.headers["X-Request-ID"]


def test_product_page_and_assets_are_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="planner-form"' in page.text
    assert 'id="weather-candidates"' in page.text
    assert 'id="map"' in page.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "new EventSource" in script.text
    assert "renderMapDay" in script.text
    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert ".planner-card" in styles.text


def test_session_message_and_pending_run(client: TestClient) -> None:
    session_id, run_id = _create_queued_run(client)
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "PENDING"
    messages = client.get(f"/v1/sessions/{session_id}/messages").json()
    assert [message["role"] for message in messages] == ["USER"]


def test_worker_executes_run_and_sse_replays_events(
    client: TestClient,
    database: Database,
) -> None:
    session_id, run_id = _create_queued_run(client)

    def fake_runner(prompt: str, *, callbacks, context) -> dict:
        assert context.usage.history_messages_used == 0
        tool_run_id = uuid.uuid4()
        for callback in callbacks:
            callback.on_tool_start(
                {"name": "find_best_weather_window"},
                "{}",
                run_id=tool_run_id,
            )
            callback.on_tool_end(
                {
                    "status": "ok",
                    "query_city": "上海",
                    "top_windows": [
                        {
                            "start_date": "2026-08-28",
                            "end_date": "2026-08-30",
                            "average_score": 88.0,
                            "dates": [
                                "2026-08-28",
                                "2026-08-29",
                                "2026-08-30",
                            ],
                        }
                    ],
                    "internal_secret": "must-not-reach-sse",
                },
                run_id=tool_run_id,
            )
        return {"answer": f"异步回答：{prompt}", "reference": []}

    assert str(run_once(database, fake_runner)) == run_id
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "SUCCEEDED"
    assert run["output"]["answer"].startswith("异步回答")
    messages = client.get(f"/v1/sessions/{session_id}/messages").json()
    assert [message["role"] for message in messages] == ["USER", "ASSISTANT"]

    stream = client.get(f"/v1/runs/{run_id}/events")
    assert stream.status_code == 200
    assert "event: RUN_QUEUED" in stream.text
    assert "event: RUN_STARTED" in stream.text
    assert "event: CONTEXT_PREPARED" in stream.text
    assert "event: TOOL_STARTED" in stream.text
    assert "event: TOOL_SUCCEEDED" in stream.text
    assert '"top_windows"' in stream.text
    assert "must-not-reach-sse" not in stream.text
    assert "event: RUN_SUCCEEDED" in stream.text

    resumed = client.get(
        f"/v1/runs/{run_id}/events",
        headers={"Last-Event-ID": "3"},
    )
    assert "event: RUN_QUEUED" not in resumed.text
    assert "event: RUN_SUCCEEDED" in resumed.text


def test_pending_run_can_be_cancelled(client: TestClient) -> None:
    _, run_id = _create_queued_run(client)
    response = client.post(f"/v1/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancel_requested"] is True


def test_worker_passes_previous_turns_to_follow_up_run(
    client: TestClient,
    database: Database,
) -> None:
    session_id, first_run_id = _create_queued_run(client)
    captured_contexts = []

    def capturing_runner(prompt: str, *, callbacks, context) -> dict:
        captured_contexts.append(context)
        return {"answer": f"回答：{prompt}", "reference": []}

    assert str(run_once(database, capturing_runner)) == first_run_id
    follow_up = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "把行程改得更轻松一些"},
    )
    assert follow_up.status_code == 202
    second_run_id = follow_up.json()["run"]["id"]
    assert str(run_once(database, capturing_runner)) == second_run_id

    assert captured_contexts[0].history == ()
    assert [message.role for message in captured_contexts[1].history] == [
        "USER",
        "ASSISTANT",
    ]
    assert [message.content for message in captured_contexts[1].history] == [
        "帮我规划近期去上海三天",
        "回答：帮我规划近期去上海三天",
    ]


def test_tool_invocation_limit_fails_without_retry(
    client: TestClient,
    database: Database,
) -> None:
    _, run_id = _create_queued_run(client)

    def excessive_runner(prompt: str, *, callbacks, context) -> dict:
        callback = callbacks[0]
        first_id = uuid.uuid4()
        callback.on_tool_start({"name": "first_tool"}, "{}", run_id=first_id)
        callback.on_tool_end({"status": "ok"}, run_id=first_id)
        callback.on_tool_start(
            {"name": "second_tool"},
            "{}",
            run_id=uuid.uuid4(),
        )
        return {"answer": prompt, "reference": []}

    assert str(run_once(database, excessive_runner, max_tool_calls=1)) == run_id
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "FAILED"
    assert run["error_code"] == "AGENT_INVOCATION_LIMIT_EXCEEDED"
    assert run["attempt_count"] == 1
    events = client.get(f"/v1/runs/{run_id}/events").text
    assert "event: CONTEXT_LIMIT_REACHED" in events
    assert "event: RUN_RETRY_SCHEDULED" not in events


def test_api_errors_use_stable_contract(client: TestClient) -> None:
    response = client.get(f"/v1/runs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error_code"] == "RUN_NOT_FOUND"
    validation = client.post("/v1/agent/invoke", json={"prompt": ""})
    assert validation.status_code == 422
    assert validation.json()["error_code"] == "VALIDATION_ERROR"
