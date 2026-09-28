"""0007 work budgets — per-Work soft/hard budgets (ACMS-REQ-059; REV2 plan §4).

New table:
  work_budgets — one row per Work Item (PK = work_item_id): soft/hard USD
  thresholds, evaluated budget_state, audited hard-limit override.

Extended (additive, nullable — never rewrite 0006 which prod already applied):
  execution_sessions.estimated_cloud_cost_usd / estimated_local_cost_usd —
  reserved split for cloud vs local inference cost accounting (REV2 §4.1).
  Both nullable: unknown stays UNKNOWN until an authoritative source exists
  (local USD-equivalent is NOT yet available — see FUTURE_WORK FW-LLM-LOCAL-COST).

Revision ID: 0007_work_budgets
Revises: 0006_memory_session_offload
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_work_budgets"
down_revision = "0006_memory_session_offload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_budgets",
        sa.Column("work_item_id", sa.String(36), primary_key=True),
        sa.Column("soft_budget_usd", sa.Float(), nullable=True),
        sa.Column("hard_budget_usd", sa.Float(), nullable=True),
        sa.Column("budget_state", sa.String(20), nullable=False, server_default="OK"),
        sa.Column("override_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("override_by", sa.String(128), nullable=True),
        sa.Column("override_reason", sa.String(512), nullable=True),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("estimated_cloud_cost_usd", sa.Float(), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("estimated_local_cost_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    # Budget rows are dropped with the table; session cost-split columns are
    # nullable telemetry extensions, so downgrade only loses the reserved split.
    op.drop_column("execution_sessions", "estimated_local_cost_usd")
    op.drop_column("execution_sessions", "estimated_cloud_cost_usd")
    op.drop_table("work_budgets")
