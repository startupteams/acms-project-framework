"""0006 memory session offload — execution_sessions, context_packages,
session_checkpoints (ACMS-REQ-055..058; flight plan Phase 8).

Revision ID: 0006_memory_session_offload
Revises: 0005_prov_attempt
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_memory_session_offload"
down_revision = "0005_prov_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("work_item_id", sa.String(36), nullable=True),
        sa.Column("assignment_id", sa.String(36), nullable=True),
        sa.Column("a2a_task_id", sa.String(255), nullable=True),
        sa.Column("harness_session_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("model_id", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("current_context_tokens", sa.Integer(), nullable=True),
        sa.Column("max_context_tokens", sa.Integer(), nullable=True),
        sa.Column("context_utilization_percent", sa.Float(), nullable=True),
        sa.Column("cumulative_api_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("rotation_advisory", sa.String(24), nullable=True),
        sa.Column("context_package_id", sa.String(36), nullable=True),
        sa.Column("last_checkpoint_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_id", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_execution_sessions_agent_id", "execution_sessions", ["agent_id"])
    op.create_index("ix_execution_sessions_work_item_id", "execution_sessions", ["work_item_id"])
    op.create_index("ix_execution_sessions_assignment_id", "execution_sessions", ["assignment_id"])
    op.create_index("ix_execution_sessions_harness_session_id", "execution_sessions", ["harness_session_id"])
    op.create_index("ix_execution_sessions_context_package_id", "execution_sessions", ["context_package_id"])
    op.create_index("ix_execution_sessions_status", "execution_sessions", ["status"])

    op.create_table(
        "context_packages",
        sa.Column("context_package_id", sa.String(36), primary_key=True),
        sa.Column("work_item_id", sa.String(36), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("assembled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=True),
        sa.Column("selected_json", sa.Text(), nullable=True),
        sa.Column("estimated_tokens", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_context_packages_work_item_id", "context_packages", ["work_item_id"])
    op.create_index("ix_context_packages_session_id", "context_packages", ["session_id"])

    op.create_table(
        "session_checkpoints",
        sa.Column("checkpoint_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("work_item_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("completeness_json", sa.Text(), nullable=True),
        sa.Column("complete", sa.Boolean(), nullable=True),
        sa.Column("next_action", sa.String(512), nullable=True),
        sa.Column("artifact_refs_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_session_checkpoints_session_id", "session_checkpoints", ["session_id"])
    op.create_index("ix_session_checkpoints_work_item_id", "session_checkpoints", ["work_item_id"])


def downgrade() -> None:
    op.drop_table("session_checkpoints")
    op.drop_table("context_packages")
    op.drop_table("execution_sessions")
