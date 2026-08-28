"""Persist W3C trace context across the API-to-Worker queue boundary.

Revision ID: 20260827_0002
Revises: 20260820_0001
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260827_0002"
down_revision: str | None = "20260820_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "trace_context_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "trace_context_json")
