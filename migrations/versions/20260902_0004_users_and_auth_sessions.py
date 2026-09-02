"""Add users, opaque auth sessions and Agent session ownership.

Revision ID: 20260902_0004
Revises: 20260901_0003
Create Date: 2026-09-02
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "20260902_0004"
down_revision: str | None = "20260901_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_sessions_token_hash",
        "auth_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_auth_sessions_expires_at",
        "auth_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_auth_sessions_user_id",
        "auth_sessions",
        ["user_id"],
        unique=False,
    )

    op.add_column(
        "agent_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO users (
                id, email, password_hash, is_active, created_at, updated_at
            )
            VALUES (
                :id, 'legacy@local.invalid', '!login-disabled!', false,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ).bindparams(
            sa.bindparam("id", value=LEGACY_USER_ID, type_=sa.Uuid())
        )
    )
    op.execute(
        sa.text(
            "UPDATE agent_sessions SET user_id = :id WHERE user_id IS NULL"
        ).bindparams(
            sa.bindparam("id", value=LEGACY_USER_ID, type_=sa.Uuid())
        )
    )
    op.alter_column("agent_sessions", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_agent_sessions_user_id_users",
        "agent_sessions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_sessions_user_id",
        "agent_sessions",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_constraint(
        "fk_agent_sessions_user_id_users",
        "agent_sessions",
        type_="foreignkey",
    )
    op.drop_column("agent_sessions", "user_id")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
