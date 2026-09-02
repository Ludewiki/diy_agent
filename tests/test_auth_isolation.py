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
        assert bob.get("/v1/sessions").json() == []

        assert alice.get(f"/v1/sessions/{session_id}").status_code == 200
        assert alice.get(f"/v1/runs/{run_id}").status_code == 200


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
