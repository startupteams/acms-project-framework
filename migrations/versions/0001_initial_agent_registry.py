"""Initial agent registry schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-26

Hand-written to match acms/models.AgentRecord exactly (ADR-0007: Alembic owns
schema; application startup must not create tables).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("external_registration_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("trust_class", sa.String(length=32), nullable=False),
        sa.Column("harness", sa.String(length=128), nullable=False),
        sa.Column("bridge_version", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.String(length=64), nullable=False),
        sa.Column("card_url", sa.String(length=2048), nullable=True),
        sa.Column("capability_hash", sa.String(length=128), nullable=True),
        sa.Column("capability_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index("ix_agents_external_registration_id", "agents", ["external_registration_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_agents_external_registration_id", table_name="agents")
    op.drop_table("agents")
