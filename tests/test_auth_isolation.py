from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.auth import hash_session_token
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import AuthSession


@pytest.fixture
def database(tmp_path) -> Database:
    path = (tmp_path / "auth-test.db").as_posix()
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    yield db
    db.dispose()


@pytest.fixture
def app(database: Database):
    return create_app(
        database=database,
        settings=Settings(
            database_url=database.url,
            sse_poll_seconds=0.01,
            sse_heartbeat_seconds=0.05,
        ),
        sync_runner=lambda prompt: {"answer": prompt, "reference": []},
    )


def _register(client: TestClient, email: str) -> str:
    csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery-staple"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    client.headers.update({"X-CSRF-Token": csrf})
    return csrf


def _create_run(client: TestClient) -> tuple[str, str]:
    session_response = client.post(
        "/v1/sessions",
        json={"title": "private trip"},
    )
    assert session_response.status_code == 201
    session_id = session_response.json()["id"]
    run_response = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "plan my private trip"},
    )
    assert run_response.status_code == 202
    return session_id, run_response.json()["run"]["id"]


def test_http_only_cookie_uses_server_side_hashed_session(
    app,
    database: Database,
) -> None:
    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        response = client.post(
            "/v1/auth/register",
            json={
                "email": "cookie@example.com",
                "password": "correct-horse-battery-staple",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 201
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        raw_token = client.cookies.get("diy_agent_session")
        assert raw_token
        with database.session_factory() as session:
            stored = session.scalar(select(AuthSession))
            assert stored is not None
            assert stored.token_hash == hash_session_token(raw_token)
            assert stored.token_hash != raw_token


def test_csrf_and_origin_are_required_for_mutations(app) -> None:
    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        payload = {
            "email": "csrf@example.com",
            "password": "correct-horse-battery-staple",
        }
        missing = client.post("/v1/auth/register", json=payload)
        assert missing.status_code == 403
        assert missing.json()["error_code"] == "CSRF_VALIDATION_FAILED"

        hostile = client.post(
            "/v1/auth/register",
            json=payload,
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://attacker.example",
            },
        )
        assert hostile.status_code == 403
        assert hostile.json()["error_code"] == "ORIGIN_NOT_ALLOWED"


def test_user_cannot_access_another_users_sessions_runs_or_sse(app) -> None:
    with TestClient(app) as alice, TestClient(app) as bob:
        _register(alice, "alice@example.com")
        _register(bob, "bob@example.com")
        session_id, run_id = _create_run(alice)

        assert bob.get(f"/v1/sessions/{session_id}").status_code == 404
        assert bob.get(f"/v1/sessions/{session_id}/messages").status_code == 404
        assert (
            bob.post(
                f"/v1/sessions/{session_id}/messages",
                json={"content": "steal this session"},
            ).status_code
            == 404
        )
        assert bob.get(f"/v1/runs/{run_id}").status_code == 404
        assert bob.post(f"/v1/runs/{run_id}/cancel").status_code == 404
        assert bob.get(f"/v1/runs/{run_id}/events").status_code == 404
        assert bob.get("/v1/sessions").json() == {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 20,
        }

        assert alice.get(f"/v1/sessions/{session_id}").status_code == 200
        assert alice.get(f"/v1/runs/{run_id}").status_code == 200


def test_session_history_metadata_pagination_and_lifecycle(app) -> None:
    with TestClient(app) as client:
        _register(client, "history@example.com")
        session_id, run_id = _create_run(client)

        history = client.get("/v1/sessions?page=1&page_size=1").json()
        assert history["total"] == 1
        assert history["page"] == 1
        assert history["page_size"] == 1
        item = history["items"][0]
        assert item["id"] == session_id
        assert item["recent_message_preview"] == "plan my private trip"
        assert item["message_count"] == 1
        assert item["last_run_id"] == run_id
        assert item["last_run_status"] == "PENDING"

        renamed = client.patch(
            f"/v1/sessions/{session_id}",
            json={"title": "renamed journey"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "renamed journey"

        archived = client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True},
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "ARCHIVED"
        assert client.get("/v1/sessions").json()["total"] == 0
        all_sessions = client.get(
            "/v1/sessions?include_archived=true"
        ).json()
        assert all_sessions["total"] == 1
        assert all_sessions["items"][0]["status"] == "ARCHIVED"

        deleted = client.delete(f"/v1/sessions/{session_id}")
        assert deleted.status_code == 204
        assert client.get(f"/v1/sessions/{session_id}").status_code == 404


def test_other_user_cannot_rename_archive_or_delete_session(app) -> None:
    with TestClient(app) as owner, TestClient(app) as attacker:
        _register(owner, "owner@example.com")
        _register(attacker, "attacker@example.com")
        session_id, _ = _create_run(owner)

        assert (
            attacker.patch(
                f"/v1/sessions/{session_id}",
                json={"title": "stolen"},
            ).status_code
            == 404
        )
        assert (
            attacker.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            ).status_code
            == 404
        )
        assert attacker.delete(f"/v1/sessions/{session_id}").status_code == 404
        assert owner.get(f"/v1/sessions/{session_id}").status_code == 200


def test_logout_revokes_the_server_side_session(app) -> None:
    with TestClient(app) as client:
        _register(client, "logout@example.com")
        assert client.get("/v1/auth/me").status_code == 200
        logout = client.post("/v1/auth/logout")
        assert logout.status_code == 204
        assert client.get("/v1/auth/me").status_code == 401
        login = client.post(
            "/v1/auth/login",
            json={
                "email": "logout@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        assert client.get("/v1/auth/me").status_code == 200


def test_unknown_credentials_do_not_reveal_account_existence(app) -> None:
    with TestClient(app) as client:
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        response = client.post(
            "/v1/auth/login",
            json={
                "email": f"{uuid.uuid4()}@example.com",
                "password": "correct-horse-battery-staple",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_CREDENTIALS"
