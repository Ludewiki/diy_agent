"""Explicit local-only demo user and legacy Session binding."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import uuid

from sqlalchemy import func, select, update

from logging_config import configure_logging
from .auth import hash_password
from .config import Settings
from .database import Database
from .models import AgentSession, User


logger = logging.getLogger(__name__)
LEGACY_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
FALLBACK_EMAIL = "admin@admin.com"
FALLBACK_PASSWORD = "123456"


@dataclass(frozen=True)
class DemoSeedResult:
    user_id: uuid.UUID | None
    email: str | None
    created: bool
    sessions_reassigned: int
    enabled: bool


def seed_demo_user(database: Database, *, enabled: bool) -> DemoSeedResult:
    if not enabled:
        return DemoSeedResult(None, None, False, 0, False)

    created = False
    with database.session_factory.begin() as session:
        demo_user = session.scalar(
            select(User)
            .where(
                User.is_active.is_(True),
                func.lower(User.email).like("%@163.com"),
            )
            .order_by(User.created_at, User.id)
            .limit(1)
        )
        if demo_user is None:
            demo_user = session.scalar(
                select(User).where(func.lower(User.email) == FALLBACK_EMAIL)
            )
        if demo_user is None:
            demo_user = User(
                email=FALLBACK_EMAIL,
                password_hash=hash_password(FALLBACK_PASSWORD),
                is_active=True,
            )
            session.add(demo_user)
            session.flush()
            created = True

        result = session.execute(
            update(AgentSession)
            .where(AgentSession.user_id == LEGACY_USER_ID)
            .values(user_id=demo_user.id)
        )
        sessions_reassigned = int(result.rowcount or 0)

    return DemoSeedResult(
        demo_user.id,
        demo_user.email,
        created,
        sessions_reassigned,
        True,
    )


def main() -> None:
    configure_logging()
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        database.require_postgresql()
        database.check_connection()
        result = seed_demo_user(database, enabled=settings.demo_user_enabled)
        logger.info(
            "demo user seed completed",
            extra={
                "event": "demo_user_seeded",
                "email": result.email,
                "user_created": result.created,
                "sessions_reassigned": result.sessions_reassigned,
                "enabled": result.enabled,
            },
        )
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
