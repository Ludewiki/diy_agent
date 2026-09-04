"""Alembic environment using the same DATABASE_URL as API and Worker."""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from app.config import Settings
from app.models import Base

PROJECT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_DIR / ".env")

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = Settings.from_env().database_url
if make_url(database_url).get_backend_name() != "postgresql":
    raise RuntimeError(
        "Alembic migration 只支持 PostgreSQL；请检查 DATABASE_URL。"
    )
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata
LANGGRAPH_MANAGED_TABLES = {"store", "store_migrations"}


def include_name(
    name: str | None,
    type_: str,
    parent_names: dict[str, str | None],
) -> bool:
    """Keep Alembic from treating LangGraph-owned Store tables as drift."""
    if type_ == "table" and name in LANGGRAPH_MANAGED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
