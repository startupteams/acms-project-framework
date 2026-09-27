"""provisioning_requests (JINT-001, REV4 §13)

Revision ID: 0004_provisioning_requests
Revises: 0003_live_telemetry
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0004_provisioning_requests"
down_revision = "0003_live_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provisioning_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=False, index=True),
        sa.Column("request_id", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("runtime_id", sa.String(64), nullable=True, index=True),
        sa.Column("state", sa.String(30), nullable=False, server_default="APPROVED"),
        sa.Column("authority", sa.String(60), nullable=False),
        sa.Column("approver", sa.String(120), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("provisioning_requests")
