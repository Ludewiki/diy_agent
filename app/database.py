"""Database engine and short-lived SQLAlchemy Session factory."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 10,
        max_overflow: int = 20,
    ) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        if url.startswith("sqlite:///./"):
            relative_path = Path(url.removeprefix("sqlite:///./"))
            relative_path.parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        engine_options: dict[str, object] = {
            "connect_args": connect_args,
            "pool_pre_ping": True,
        }
        if not url.startswith("sqlite"):
            engine_options.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=30,
                pool_recycle=1800,
            )
        self.engine: Engine = create_engine(url, **engine_options)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_schema(self) -> None:
        """Create tables for isolated SQLite tests only.

        PostgreSQL schemas are owned by Alembic and must never be changed at
        application startup.
        """
        if not self.is_sqlite:
            raise RuntimeError("PostgreSQL 表结构必须通过 Alembic migration 创建。")
        Base.metadata.create_all(self.engine)

    @property
    def is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    @property
    def is_postgresql(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def check_connection(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def require_postgresql(self) -> None:
        if not self.is_postgresql:
            raise RuntimeError(
                "API 和 Worker 运行时要求 PostgreSQL；SQLite 仅用于隔离单元测试。"
            )

    def dispose(self) -> None:
        self.engine.dispose()
