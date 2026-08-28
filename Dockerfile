FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN groupadd --system app && \
    useradd --system --gid app --create-home --home-dir /home/app app

COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY travel_planner ./travel_planner
COPY migrations ./migrations
COPY alembic.ini logging_config.py tool_errors.py ./
COPY weather_tool.py weather_window.py travel_planner_tool.py ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable && \
    chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
