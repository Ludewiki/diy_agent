from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import AgentRun, RunStatus
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
        csrf_response = test_client.get("/v1/auth/csrf")
        csrf_token = csrf_response.json()["csrf_token"]
        register_response = test_client.post(
            "/v1/auth/register",
            json={
                "email": "api-worker@example.com",
                "password": "correct-horse-battery-staple",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert register_response.status_code == 201
        test_client.headers.update({"X-CSRF-Token": csrf_token})
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
    assert "/static/app.js?v=20260903-workbench" in page.text
    assert 'id="history-dialog"' in page.text
    assert 'id="workspace-session-list"' in page.text
    assert 'id="conversation-list"' in page.text
    assert 'id="follow-up-form"' in page.text
    assert 'maxlength="20000"' in page.text
    assert page.headers["cache-control"] == "no-cache"
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "new EventSource" in script.text
    assert "renderMapDay" in script.text
    assert "submitFollowUp" in script.text
    assert '"/messages"' in script.text
    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert ".planner-card" in styles.text
    assert ".workbench-grid" in styles.text
    assert ".message-bubble" in styles.text


def test_session_message_and_pending_run(client: TestClient) -> None:
    session_id, run_id = _create_queued_run(client)
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "PENDING"
    messages = client.get(f"/v1/sessions/{session_id}/messages").json()
    assert [message["role"] for message in messages] == ["USER"]


def test_follow_up_result_inherits_request_and_previous_artifacts(
    client: TestClient,
    database: Database,
) -> None:
    session = client.post("/v1/sessions", json={"title": "上海慢旅行"}).json()
    first = client.post(
        f"/v1/sessions/{session['id']}/messages",
        json={
            "content": "规划上海三天慢旅行",
            "planning_context": {
                "city": "上海",
                "trip_days": 3,
                "interests": ["历史建筑", "城市漫步"],
                "budget": "适中预算",
                "additional_preferences": "每天十点后出发",
            },
        },
    )
    assert first.status_code == 202
    first_run_id = first.json()["run"]["id"]

    def first_runner(prompt: str, *, callbacks, context) -> dict:
        tool_run_id = uuid.uuid4()
        callbacks[0].on_tool_start(
            {"name": "find_best_weather_window"}, "{}", run_id=tool_run_id
        )
        callbacks[0].on_tool_end(
            {
                "status": "ok",
                "query_city": "上海",
                "best_window": {"dates": ["2026-09-08", "2026-09-09", "2026-09-10"]},
                "top_windows": [],
                "source": {"name": "Open-Meteo", "forecast_url": "https://api.open-meteo.com/"},
            },
            run_id=tool_run_id,
        )
        return {"answer": "第一版行程", "reference": []}

    assert str(run_once(database, first_runner)) == first_run_id
    follow_up = client.post(
        f"/v1/sessions/{session['id']}/messages",
        json={"content": "为什么选择这三天？"},
    )
    assert follow_up.status_code == 202
    output = follow_up.json()["run"]["output"]
    assert output["plan_revision"] == 2
    assert output["supersedes_run_id"] == first_run_id
    assert output["request"] == {
        "message": "为什么选择这三天？",
        "city": "上海",
        "trip_days": 3,
        "interests": ["历史建筑", "城市漫步"],
        "budget": "适中预算",
        "additional_preferences": "每天十点后出发",
    }
    assert output["weather_window"]["query_city"] == "上海"
    assert (
        output["components"]["weather"]["inherited_from_run_id"]
        == first_run_id
    )

    second_run_id = follow_up.json()["run"]["id"]

    def failed_weather_recheck(prompt: str, *, callbacks, context) -> dict:
        tool_run_id = uuid.uuid4()
        callbacks[0].on_tool_start(
            {"name": "find_best_weather_window"}, "{}", run_id=tool_run_id
        )
        callbacks[0].on_tool_end(
            {
                "status": "error",
                "error_code": "WEATHER_UPSTREAM_FAILED",
                "message": "天气服务暂时不可用。",
                "retryable": False,
            },
            run_id=tool_run_id,
        )
        raise AssertionError("Tool 失败后不应继续")

    assert str(run_once(database, failed_weather_recheck)) == second_run_id
    failed_output = client.get(f"/v1/runs/{second_run_id}").json()["output"]
    assert failed_output["result_status"] == "failed"
    assert failed_output["weather_window"]["query_city"] == "上海"
    assert failed_output["components"]["weather"]["status"] == "degraded"
    assert (
        failed_output["components"]["weather"]["inherited_from_run_id"]
        == first_run_id
    )


def test_failed_run_keeps_completed_weather_snapshot(
    client: TestClient,
    database: Database,
) -> None:
    _, run_id = _create_queued_run(client)

    def partial_runner(prompt: str, *, callbacks, context) -> dict:
        weather_id = uuid.uuid4()
        callbacks[0].on_tool_start(
            {"name": "find_best_weather_window"}, "{}", run_id=weather_id
        )
        callbacks[0].on_tool_end(
            {
                "status": "ok",
                "query_city": "上海",
                "best_window": {"dates": ["2026-09-08", "2026-09-09", "2026-09-10"]},
                "top_windows": [],
            },
            run_id=weather_id,
        )
        plan_id = uuid.uuid4()
        callbacks[0].on_tool_start(
            {"name": "plan_wikivoyage_trip"}, "{}", run_id=plan_id
        )
        callbacks[0].on_tool_end(
            {
                "status": "error",
                "error_code": "ORS_AUTH_FAILED",
                "message": "路线服务鉴权失败。",
                "retryable": False,
            },
            run_id=plan_id,
        )
        raise AssertionError("Tool 失败后不应继续")

    assert str(run_once(database, partial_runner)) == run_id
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "FAILED"
    assert run["output"]["result_status"] == "failed"
    assert run["output"]["weather_window"]["query_city"] == "上海"
    assert run["output"]["components"]["weather"]["status"] == "ready"
    assert run["output"]["components"]["guide"]["status"] == "unavailable"
    assert run["output"]["components"]["route"]["status"] == "unavailable"
    assert any(
        warning["code"] == "ORS_AUTH_FAILED"
        for warning in run["output"]["warnings"]
    )


def test_run_query_upgrades_legacy_output(
    client: TestClient,
    database: Database,
) -> None:
    _, run_id = _create_queued_run(client)
    with database.session_factory.begin() as session:
        run = session.get(AgentRun, uuid.UUID(run_id))
        assert run is not None
        run.status = RunStatus.SUCCEEDED.value
        run.output_json = {
            "answer": "旧版回答",
            "reference": [
                {
                    "title": "旧攻略" + ("长" * 600),
                    "url": "https://example.test/legacy",
                }
            ],
        }

    output = client.get(f"/v1/runs/{run_id}").json()["output"]
    assert output["schema_version"] == "1.0"
    assert output["result_status"] == "partial"
    assert output["assistant_answer"] == "旧版回答"
    assert output["sources"][0]["provider"] == "legacy"
    assert output["sources"][0]["title"].startswith("旧攻略")
    assert len(output["sources"][0]["title"]) == 500


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
            plan_tool_run_id = uuid.uuid4()
            callback.on_tool_start(
                {"name": "plan_wikivoyage_trip"},
                "{}",
                run_id=plan_tool_run_id,
            )
            callback.on_tool_end(
                {
                    "status": "ok",
                    "city": "上海",
                    "trip_days": 3,
                    "map_profile": "foot-walking",
                    "itinerary": [{"date": "2026-08-28", "attractions": []}],
                    "source_pages": [
                        {"title": "上海", "url": "https://example.test/shanghai"}
                    ],
                    "attribution": {"content_source": "Wikivoyage contributors"},
                    "warnings": [],
                    "private_debug": "must-not-reach-output",
                },
                run_id=plan_tool_run_id,
            )
        return {"answer": f"异步回答：{prompt}", "reference": []}

    assert str(run_once(database, fake_runner)) == run_id
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "SUCCEEDED"
    assert run["output"]["schema_version"] == "1.0"
    assert run["output"]["result_status"] == "complete"
    assert run["output"]["assistant_answer"].startswith("异步回答")
    assert run["output"]["weather_window"]["query_city"] == "上海"
    assert run["output"]["components"]["weather"]["status"] == "ready"
    assert run["output"]["components"]["guide"]["status"] == "ready"
    assert run["output"]["components"]["route"]["status"] == "ready"
    assert run["output"]["itinerary"]["city"] == "上海"
    assert "private_debug" not in run["output"]["itinerary"]
    assert {source["provider"] for source in run["output"]["sources"]} >= {
        "Wikivoyage",
        "OpenRouteService",
    }
    assert run["output"]["context_usage"]["history_messages_used"] == 0
    messages = client.get(f"/v1/sessions/{session_id}/messages").json()
    assert [message["role"] for message in messages] == ["USER", "ASSISTANT"]

    stream = client.get(f"/v1/runs/{run_id}/events")
    assert stream.status_code == 200
    assert "event: RUN_QUEUED" in stream.text
    assert "event: RUN_STARTED" in stream.text
    assert "event: CONTEXT_PREPARED" in stream.text
    assert "event: TOOL_STARTED" in stream.text
    assert "event: TOOL_SUCCEEDED" in stream.text
    assert '"top_windows"' not in stream.text
    assert '"result"' not in stream.text
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
    assert response.json()["error_code"] == "RUN_CANCELLED"
    assert response.json()["output"]["result_status"] == "failed"


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


def test_tool_error_stops_agent_loop_immediately(
    client: TestClient,
    database: Database,
) -> None:
    _, run_id = _create_queued_run(client)
    tool_calls = 0

    def failing_runner(prompt: str, *, callbacks, context) -> dict:
        nonlocal tool_calls
        callback = callbacks[0]
        tool_calls += 1
        tool_run_id = uuid.uuid4()
        callback.on_tool_start(
            {"name": "plan_wikivoyage_trip"},
            "{}",
            run_id=tool_run_id,
        )
        callback.on_tool_end(
            {
                "status": "error",
                "error_code": "ORS_AUTH_FAILED",
                "message": "OpenRouteService 鉴权失败。",
                "retryable": False,
            },
            run_id=tool_run_id,
        )
        raise AssertionError("Tool error 后不应继续执行 Agent")

    assert str(run_once(database, failing_runner)) == run_id
    run = client.get(f"/v1/runs/{run_id}").json()
    assert tool_calls == 1
    assert run["status"] == "FAILED"
    assert run["error_code"] == "ORS_AUTH_FAILED"
    assert run["error_message"] == "OpenRouteService 鉴权失败。"
    events = client.get(f"/v1/runs/{run_id}/events").text
    assert events.count("event: TOOL_FAILED") == 1
    assert "event: CONTEXT_LIMIT_REACHED" not in events


def test_api_errors_use_stable_contract(client: TestClient) -> None:
    response = client.get(f"/v1/runs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error_code"] == "RUN_NOT_FOUND"
    validation = client.post("/v1/agent/invoke", json={"prompt": ""})
    assert validation.status_code == 422
    assert validation.json()["error_code"] == "VALIDATION_ERROR"
