"""0016: power_cost_snapshot (STEA-004 plan §17/§32).

Normalized facility summaries ingested from LLM Manager. Additive table only.
Revision ID: 0016_power_snapshot
Revises: 0015_repositories_slop
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_power_snapshot"
down_revision = "0015_repositories_slop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "power_cost_snapshot" not in tables:
        op.create_table(
            "power_cost_snapshot",
            sa.Column("snapshot_id", sa.String(length=36), primary_key=True),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("payload_json", sa.String(length=512), nullable=True),
            sa.Column("total_kwh_24h", sa.Float(), nullable=True),
            sa.Column("total_cost_usd_24h", sa.Float(), nullable=True),
            sa.Column("total_kwh_30d", sa.Float(), nullable=True),
            sa.Column("total_cost_usd_30d", sa.Float(), nullable=True),
            sa.Column("current_watts", sa.Float(), nullable=True),
            sa.Column("collector_stale", sa.Boolean(), nullable=True),
            sa.Column("last_sample_ts", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "power_cost_snapshot" in tables:
        op.drop_table("power_cost_snapshot")