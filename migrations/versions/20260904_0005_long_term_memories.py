"""Add governed long-term travel memories.

Revision ID: 20260904_0005
Revises: 20260902_0004
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260904_0005"
down_revision: str | None = "20260902_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "travel_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("source_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["agent_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "normalized_key",
            name="uq_travel_memories_user_key",
        ),
    )
    op.create_index(
        "ix_travel_memories_user_id",
        "travel_memories",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_travel_memories_memory_type",
        "travel_memories",
        ["memory_type"],
        unique=False,
    )
    op.create_index(
        "ix_travel_memories_status",
        "travel_memories",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_travel_memories_user_status_updated",
        "travel_memories",
        ["user_id", "status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_travel_memories_user_status_updated",
        table_name="travel_memories",
    )
    op.drop_index("ix_travel_memories_status", table_name="travel_memories")
    op.drop_index("ix_travel_memories_memory_type", table_name="travel_memories")
    op.drop_index("ix_travel_memories_user_id", table_name="travel_memories")
    op.drop_table("travel_memories")
