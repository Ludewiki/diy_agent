"""FastAPI application exposing synchronous and durable Agent execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import secrets
import time
from typing import Any, AsyncIterator, Callable
import uuid
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from opentelemetry import trace

from logging_config import configure_logging
from weather_window import run_prompt
from .auth import AuthService, tokens_match
from .config import Settings
from .database import Database
from .errors import ApiError
from .models import (
    AgentRun,
    AgentSession,
    Message,
    RunStatus,
    TERMINAL_RUN_STATUSES,
    User,
)
from .schemas import (
    AuthCredentials,
    CsrfResponse,
    HealthResponse,
    InvokeRequest,
    InvokeResponse,
    MessageCreate,
    MessageResponse,
    QueuedRunResponse,
    RunResponse,
    SessionCreate,
    SessionResponse,
    UserResponse,
)
from .store import (
    enqueue_message,
    read_events_after,
    request_cancellation,
    run_to_dict,
)
from .telemetry import (
    current_trace_id,
    record_http_request,
    runtime as telemetry_runtime,
)

logger = logging.getLogger(__name__)
SyncRunner = Callable[..., Any]
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"


def _answer_dict(answer: Any) -> dict[str, Any]:
    if isinstance(answer, BaseModel):
        result = answer.model_dump(mode="json")
    elif isinstance(answer, dict):
        result = answer
    else:
        result = {"answer": str(answer), "reference": []}
    result.setdefault("answer", "")
    result.setdefault("reference", [])
    return result


def _error_payload(
    request: Request,
    error_code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": error_code,
        "message": message,
        "details": details or {},
        "request_id": getattr(request.state, "request_id", None),
        "trace_id": current_trace_id(),
    }


def _format_sse(event: dict[str, Any]) -> str:
    payload = {
        "sequence": event["sequence"],
        "type": event["event_type"],
        "data": event["data"],
        "created_at": event["created_at"],
    }
    return (
        f"id: {event['sequence']}\n"
        f"event: {event['event_type']}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def create_app(
    *,
    database: Database | None = None,
    settings: Settings | None = None,
    sync_runner: SyncRunner = run_prompt,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    runtime_settings.validate()
    runtime_database = database or Database(runtime_settings.database_url)
    if database is None:
        runtime_database.require_postgresql()
    auth_service = AuthService(runtime_database, runtime_settings)
    telemetry_runtime.configure(
        runtime_settings,
        service_role="api",
        database=runtime_database,
    )
    telemetry_runtime.register_pending_runs_gauge(runtime_database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        runtime_database.check_connection()
        try:
            yield
        finally:
            telemetry_runtime.shutdown()
            runtime_database.dispose()

    application = FastAPI(
        title="Weather-aware Travel Planner Agent API",
        version="0.7.0",
        description="Web 产品入口、PostgreSQL 会话、租约 Worker、重试与 SSE 进度事件。",
        lifespan=lifespan,
    )
    application.state.database = runtime_database
    application.state.settings = runtime_settings
    application.state.sync_runner = sync_runner
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Content-Type",
            "Last-Event-ID",
            "X-CSRF-Token",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID", "X-Trace-ID", "Server-Timing"],
    )
    application.mount(
        "/static",
        StaticFiles(directory=WEB_DIRECTORY),
        name="static",
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next: Callable[..., Any]) -> Any:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - started
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", request.url.path)
            record_http_request(
                duration,
                method=request.method,
                route=route,
                status_code=status_code,
            )
            trace_id = current_trace_id()
            if response is not None:
                response.headers["X-Request-ID"] = request_id
                if trace_id is not None:
                    response.headers["X-Trace-ID"] = trace_id
                response.headers["Server-Timing"] = f"app;dur={duration * 1000:.2f}"
            current_span = trace.get_current_span()
            if current_span.is_recording():
                current_span.set_attribute("request.id", request_id)
            logger.info(
                "http request completed",
                extra={"event": "http_request", "request_id": request_id},
            )

    @application.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(request, exc.error_code, exc.message, exc.details),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_payload(
                request,
                "VALIDATION_ERROR",
                "请求参数校验失败。",
                {"errors": exc.errors()},
            ),
        )

    def require_csrf(request: Request) -> None:
        origin = request.headers.get("Origin")
        if origin:
            request_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
            allowed_origins = {
                request_origin,
                *(item.rstrip("/") for item in runtime_settings.csrf_trusted_origins),
            }
            if origin.rstrip("/") not in allowed_origins:
                raise ApiError(
                    403,
                    "ORIGIN_NOT_ALLOWED",
                    "请求来源不受信任。",
                )
        if not tokens_match(
            request.cookies.get(runtime_settings.csrf_cookie_name),
            request.headers.get("X-CSRF-Token"),
        ):
            raise ApiError(
                403,
                "CSRF_VALIDATION_FAILED",
                "CSRF 校验失败，请刷新页面后重试。",
            )

    def current_user(
        raw_token: str | None = Cookie(default=None, alias=runtime_settings.auth_cookie_name),
    ) -> User:
        return auth_service.current_user(raw_token)

    def set_auth_cookie(response: Response, raw_token: str) -> None:
        max_age = runtime_settings.auth_session_lifetime_days * 24 * 60 * 60
        response.set_cookie(
            key=runtime_settings.auth_cookie_name,
            value=raw_token,
            max_age=max_age,
            httponly=True,
            secure=runtime_settings.auth_cookie_secure,
            samesite="lax",
            path="/",
        )

    @application.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        runtime_database.check_connection()
        return HealthResponse(status="ok", database="reachable")

    @application.get("/", include_in_schema=False)
    def product_page() -> FileResponse:
        return FileResponse(
            WEB_DIRECTORY / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @application.get(
        "/v1/auth/csrf",
        response_model=CsrfResponse,
        tags=["auth"],
    )
    def issue_csrf_token(response: Response) -> CsrfResponse:
        token = secrets.token_urlsafe(32)
        response.set_cookie(
            key=runtime_settings.csrf_cookie_name,
            value=token,
            httponly=False,
            secure=runtime_settings.auth_cookie_secure,
            samesite="lax",
            path="/",
        )
        return CsrfResponse(csrf_token=token)

    @application.post(
        "/v1/auth/register",
        response_model=UserResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["auth"],
        dependencies=[Depends(require_csrf)],
    )
    def register(payload: AuthCredentials, response: Response) -> User:
        user, raw_token = auth_service.register(payload.email, payload.password)
        set_auth_cookie(response, raw_token)
        return user

    @application.post(
        "/v1/auth/login",
        response_model=UserResponse,
        tags=["auth"],
        dependencies=[Depends(require_csrf)],
    )
    def login(payload: AuthCredentials, response: Response) -> User:
        user, raw_token = auth_service.login(payload.email, payload.password)
        set_auth_cookie(response, raw_token)
        return user

    @application.get(
        "/v1/auth/me",
        response_model=UserResponse,
        tags=["auth"],
    )
    def me(user: User = Depends(current_user)) -> User:
        return user

    @application.post(
        "/v1/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["auth"],
        dependencies=[Depends(require_csrf)],
    )
    def logout(
        response: Response,
        raw_token: str | None = Cookie(
            default=None,
            alias=runtime_settings.auth_cookie_name,
        ),
        _user: User = Depends(current_user),
    ) -> Response:
        auth_service.revoke(raw_token)
        response.delete_cookie(
            runtime_settings.auth_cookie_name,
            path="/",
            secure=runtime_settings.auth_cookie_secure,
            httponly=True,
            samesite="lax",
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @application.post(
        "/v1/agent/invoke",
        response_model=InvokeResponse,
        tags=["agent"],
        summary="同步执行 Agent（开发和演示用途）",
    )
    def invoke_agent(
        payload: InvokeRequest,
        _user: User = Depends(current_user),
        _csrf: None = Depends(require_csrf),
    ) -> InvokeResponse:
        with telemetry_runtime.tracer.start_as_current_span(
            "agent.invoke",
            attributes={"agent.run.mode": "synchronous"},
        ):
            try:
                result = _answer_dict(sync_runner(payload.prompt))
            except RuntimeError as exc:
                raise ApiError(503, "AGENT_UNAVAILABLE", str(exc)) from exc
            except Exception as exc:
                logger.error(
                    "synchronous agent invocation failed",
                    extra={"event": "run_failed", "error_code": "AGENT_EXECUTION_FAILED"},
                )
                raise ApiError(
                    502,
                    "AGENT_EXECUTION_FAILED",
                    "Agent 执行失败，请使用 X-Request-ID 查询服务日志。",
                ) from exc
        return InvokeResponse.model_validate(result)

    @application.post(
        "/v1/sessions",
        response_model=SessionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["sessions"],
    )
    def create_session(
        payload: SessionCreate,
        user: User = Depends(current_user),
        _csrf: None = Depends(require_csrf),
    ) -> AgentSession:
        with telemetry_runtime.tracer.start_as_current_span(
            "agent.session.create"
        ) as span:
            with runtime_database.session_factory.begin() as session:
                conversation = AgentSession(user_id=user.id, title=payload.title)
                session.add(conversation)
                session.flush()
                span.set_attribute("agent.session.id", str(conversation.id))
                return conversation

    @application.get(
        "/v1/sessions",
        response_model=list[SessionResponse],
        tags=["sessions"],
    )
    def list_sessions(user: User = Depends(current_user)) -> list[AgentSession]:
        with runtime_database.session_factory() as session:
            return list(
                session.scalars(
                    select(AgentSession)
                    .where(AgentSession.user_id == user.id)
                    .order_by(AgentSession.updated_at.desc(), AgentSession.id)
                )
            )

    @application.get(
        "/v1/sessions/{session_id}",
        response_model=SessionResponse,
        tags=["sessions"],
    )
    def get_session(
        session_id: uuid.UUID,
        user: User = Depends(current_user),
    ) -> AgentSession:
        with runtime_database.session_factory() as session:
            conversation = session.scalar(
                select(AgentSession).where(
                    AgentSession.id == session_id,
                    AgentSession.user_id == user.id,
                )
            )
            if conversation is None:
                raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在。")
            return conversation

    @application.get(
        "/v1/sessions/{session_id}/messages",
        response_model=list[MessageResponse],
        tags=["sessions"],
    )
    def list_messages(
        session_id: uuid.UUID,
        user: User = Depends(current_user),
    ) -> list[Message]:
        with runtime_database.session_factory() as session:
            conversation = session.scalar(
                select(AgentSession).where(
                    AgentSession.id == session_id,
                    AgentSession.user_id == user.id,
                )
            )
            if conversation is None:
                raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在。")
            return list(
                session.scalars(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.created_at, Message.id)
                )
            )

    @application.post(
        "/v1/sessions/{session_id}/messages",
        response_model=QueuedRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["runs"],
        summary="保存消息并创建异步 Agent Run",
    )
    def submit_message(
        session_id: uuid.UUID,
        payload: MessageCreate,
        request: Request,
        user: User = Depends(current_user),
        _csrf: None = Depends(require_csrf),
    ) -> QueuedRunResponse:
        with telemetry_runtime.tracer.start_as_current_span(
            "agent.session.load",
            attributes={"agent.session.id": str(session_id)},
        ):
            with telemetry_runtime.tracer.start_as_current_span(
                "agent.run.enqueue",
                attributes={"agent.session.id": str(session_id)},
            ) as span:
                queued = enqueue_message(
                    runtime_database,
                    session_id,
                    user.id,
                    payload.content,
                    max_attempts=runtime_settings.worker_max_attempts,
                )
                if queued is not None:
                    span.set_attribute("agent.run.id", str(queued[1].id))
        if queued is None:
            raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在。")
        message, run = queued
        return QueuedRunResponse(
            message=MessageResponse.model_validate(message),
            run=RunResponse.model_validate(run_to_dict(run)),
            events_url=str(request.url_for("stream_run_events", run_id=run.id)),
        )

    @application.get(
        "/v1/runs/{run_id}",
        response_model=RunResponse,
        tags=["runs"],
    )
    def get_run(
        run_id: uuid.UUID,
        user: User = Depends(current_user),
    ) -> RunResponse:
        with runtime_database.session_factory() as session:
            run = session.scalar(
                select(AgentRun)
                .join(AgentSession, AgentSession.id == AgentRun.session_id)
                .where(
                    AgentRun.id == run_id,
                    AgentSession.user_id == user.id,
                )
            )
            if run is None:
                raise ApiError(404, "RUN_NOT_FOUND", "Agent Run 不存在。")
            return RunResponse.model_validate(run_to_dict(run))

    @application.post(
        "/v1/runs/{run_id}/cancel",
        response_model=RunResponse,
        tags=["runs"],
    )
    def cancel_run(
        run_id: uuid.UUID,
        user: User = Depends(current_user),
        _csrf: None = Depends(require_csrf),
    ) -> RunResponse:
        run = request_cancellation(runtime_database, run_id, user.id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Agent Run 不存在。")
        return RunResponse.model_validate(run_to_dict(run))

    @application.get(
        "/v1/runs/{run_id}/events",
        name="stream_run_events",
        tags=["runs"],
        summary="通过 Server-Sent Events 推送持久化进度",
    )
    async def stream_run_events(
        run_id: uuid.UUID,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        user: User = Depends(current_user),
    ) -> StreamingResponse:
        with runtime_database.session_factory() as session:
            run = session.scalar(
                select(AgentRun)
                .join(AgentSession, AgentSession.id == AgentRun.session_id)
                .where(
                    AgentRun.id == run_id,
                    AgentSession.user_id == user.id,
                )
            )
            if run is None:
                raise ApiError(404, "RUN_NOT_FOUND", "Agent Run 不存在。")
        cursor = after
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise ApiError(400, "INVALID_LAST_EVENT_ID", "Last-Event-ID 必须是整数。") from exc

        async def event_stream() -> AsyncIterator[str]:
            nonlocal cursor
            last_write = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                events, run_status = await asyncio.to_thread(
                    read_events_after,
                    runtime_database,
                    run_id,
                    cursor,
                )
                for event in events:
                    cursor = event["sequence"]
                    last_write = time.monotonic()
                    yield _format_sse(event)
                if run_status in TERMINAL_RUN_STATUSES:
                    return
                if time.monotonic() - last_write >= runtime_settings.sse_heartbeat_seconds:
                    last_write = time.monotonic()
                    yield ": heartbeat\n\n"
                await asyncio.sleep(runtime_settings.sse_poll_seconds)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    telemetry_runtime.instrument_fastapi(application)
    return application


app = create_app()
