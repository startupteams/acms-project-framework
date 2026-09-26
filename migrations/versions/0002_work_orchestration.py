"""Work orchestration schema: work items, assignments, execution tasks,
background routines, handoffs (ACMS-REQ-007/008/013/014/036).

Revision ID: 0002_work_orchestration
Revises: 0001_initial
Create Date: 2026-09-26

Hand-written to match acms/work_models.py exactly (ADR-0007: Alembic owns
schema; application startup must not create tables).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_work_orchestration"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "work_items",
        sa.Column("work_item_id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("scope_markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["work_items.work_item_id"], ondelete="SET NULL"),
    )
    op.create_index("ix_work_items_parent_id", "work_items", ["parent_id"])

    op.create_table(
        "work_assignments",
        sa.Column("assignment_id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_items.work_item_id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("assigned_by", sa.String(128), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_work_assignments_agent_id", "work_assignments", ["agent_id"])
    op.create_index("ix_work_assignments_work_item_id", "work_assignments", ["work_item_id"])
    op.create_index("ix_work_assignments_status", "work_assignments", ["status"])

    op.create_table(
        "execution_tasks",
        sa.Column("task_id", sa.String(36), primary_key=True),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_items.work_item_id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=True),
        sa.Column("external_task_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_execution_tasks_work_item_id", "execution_tasks", ["work_item_id"])
    op.create_index("ix_execution_tasks_agent_id", "execution_tasks", ["agent_id"])
    op.create_index("ix_execution_tasks_external_task_id", "execution_tasks", ["external_task_id"])

    op.create_table(
        "background_routines",
        sa.Column("routine_id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("schedule", sa.String(128), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("latest_status", sa.String(255), nullable=True),
        sa.Column("latest_report_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_background_routines_agent_id", "background_routines", ["agent_id"])

    op.create_table(
        "work_handoffs",
        sa.Column("handoff_id", sa.String(36), primary_key=True),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_items.work_item_id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_work_handoffs_work_item_id", "work_handoffs", ["work_item_id"])


def downgrade() -> None:
    op.drop_table("work_handoffs")
    op.drop_table("background_routines")
    op.drop_table("execution_tasks")
    op.drop_table("work_assignments")
    op.drop_table("work_items")
