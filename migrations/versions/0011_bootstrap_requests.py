"""0011: product-bootstrap requests (window-5 plan §9.5).

Single additive table ``bootstrap_requests`` — resumable per-step durable
results for idea→repo→docs→hierarchy→Jira bootstrap. SQLite + PostgreSQL safe
(covered by the PG chain + parity tests).

Revision ID: 0011_bootstrap_requests
Revises: 0010_jira_kickoff_gate
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_bootstrap_requests"
down_revision = "0010_jira_kickoff_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bootstrap_requests",
        sa.Column("request_id", sa.String(length=36), primary_key=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("product_name", sa.String(length=255), nullable=True),
        sa.Column("steps_json", sa.Text(), nullable=True),
        sa.Column("idea_content_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bootstrap_requests_state", "bootstrap_requests", ["state"])


def downgrade() -> None:
    op.drop_index("ix_bootstrap_requests_state", table_name="bootstrap_requests")
    op.drop_table("bootstrap_requests")