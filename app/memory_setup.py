"""One-shot deployment command for LangGraph PostgresStore tables."""

from __future__ import annotations

from dotenv import load_dotenv

from logging_config import configure_logging
from .config import Settings
from .database import Database
from .memory import LangGraphPostgresStoreBackend, MemoryService


def main() -> int:
    load_dotenv(".env")
    configure_logging()
    settings = Settings.from_env()
    settings.validate()
    database = Database(settings.database_url)
    database.require_postgresql()
    try:
        service = MemoryService(
            database,
            LangGraphPostgresStoreBackend(settings.database_url),
            recall_limit=settings.memory_recall_limit,
            auto_extract=settings.memory_auto_extract,
        )
        service.setup_store()
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
