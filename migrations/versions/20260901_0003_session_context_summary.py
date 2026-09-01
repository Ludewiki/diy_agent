"""Persist rolling short-term-memory summaries on Agent sessions.

Revision ID: 20260901_0003
Revises: 20260827_0002
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0003"
down_revision: str | None = "20260827_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("summary_text", sa.Text(), nullable=True))
    op.add_column(
        "agent_sessions",
        sa.Column("summary_through_message_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column(
            "summary_message_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_sessions",
        sa.Column(
            "summary_token_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("summary_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "summary_updated_at")
    op.drop_column("agent_sessions", "summary_token_count")
    op.drop_column("agent_sessions", "summary_message_count")
    op.drop_column("agent_sessions", "summary_through_message_id")
    op.drop_column("agent_sessions", "summary_text")
