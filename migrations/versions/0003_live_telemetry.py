"""Live fleet telemetry + human-readable keys (combined slice 3+4; ADR-0010;
ACMS-REQ-033/034/035/052/053/054).

Revision ID: 0003_live_telemetry
Revises: 0002_work_orchestration
Create Date: 2026-09-26

Add-only/backward-compatible (plan §24, §19): no drops, no destructive renames.
Hand-written per ADR-0007 to match acms/telemetry_models.py + key columns in
acms/work_models.py.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_live_telemetry"
down_revision: Union[str, None] = "0002_work_orchestration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- human-readable keys (REQ-052): add-only columns + durable counters ---
    op.add_column("work_items", sa.Column("work_key", sa.String(32), nullable=True))
    op.create_index("ix_work_items_work_key", "work_items", ["work_key"], unique=True)
    op.add_column("work_assignments", sa.Column("assignment_key", sa.String(32), nullable=True))
    op.create_index("ix_work_assignments_assignment_key", "work_assignments", ["assignment_key"], unique=True)
    op.add_column("work_assignments", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("work_assignments", sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("work_assignments", sa.Column("bound_session_id", sa.String(255), nullable=True))

    op.create_table(
        "acms_key_counters",
        sa.Column("counter_name", sa.String(64), primary_key=True),
        sa.Column("counter_value", sa.Integer(), nullable=False),
    )

    # --- live status (one row per agent, updated every heartbeat) ---
    op.create_table(
        "agent_status_current",
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
        sa.Column("connectivity", sa.String(16), nullable=False, server_default="UNKNOWN"),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_contact_source", sa.String(32), nullable=True),
        sa.Column("stale_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_running", sa.String(16), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("session_title", sa.String(255), nullable=True),
        sa.Column("expected_work_key", sa.String(64), nullable=True),
        sa.Column("expected_assignment_key", sa.String(64), nullable=True),
        sa.Column("alignment", sa.String(16), nullable=True),
        sa.Column("model_id", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("context_used_tokens", sa.Integer(), nullable=True),
        sa.Column("context_max_tokens", sa.Integer(), nullable=True),
        sa.Column("context_utilization_percent", sa.Float(), nullable=True),
        sa.Column("context_telemetry_valid", sa.Boolean(), nullable=True),
        sa.Column("context_warning", sa.String(16), nullable=True),
        sa.Column("context_max_source", sa.String(64), nullable=True),
        sa.Column("session_total_tokens", sa.Integer(), nullable=True),
        sa.Column("cumulative_api_tokens", sa.Integer(), nullable=True),
        sa.Column("bridge_id", sa.String(128), nullable=True),
        sa.Column("bridge_version", sa.String(64), nullable=True),
        sa.Column("harness_version", sa.String(128), nullable=True),
        sa.Column("platforms_connected", sa.String(255), nullable=True),
        sa.Column("heartbeat_schema_version", sa.String(32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- telemetry history (plan §8: periodic samples + on material change) ---
    op.create_table(
        "agent_telemetry_samples",
        sa.Column("sample_id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_running", sa.String(16), nullable=True),
        sa.Column("connectivity", sa.String(16), nullable=True),
        sa.Column("model_id", sa.String(255), nullable=True),
        sa.Column("context_used_tokens", sa.Integer(), nullable=True),
        sa.Column("context_max_tokens", sa.Integer(), nullable=True),
        sa.Column("context_utilization_percent", sa.Float(), nullable=True),
        sa.Column("context_warning", sa.String(16), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("session_title", sa.String(255), nullable=True),
        sa.Column("session_total_tokens", sa.Integer(), nullable=True),
        sa.Column("cumulative_api_tokens", sa.Integer(), nullable=True),
    )
    op.create_index("ix_agent_telemetry_samples_agent_id", "agent_telemetry_samples", ["agent_id"])

    # --- semantic events (REQ-035) ---
    op.create_table(
        "agent_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_source", sa.String(128), nullable=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=True),
        sa.Column("work_key", sa.String(64), nullable=True),
        sa.Column("assignment_key", sa.String(64), nullable=True),
        sa.Column("a2a_task_id", sa.String(255), nullable=True),
        sa.Column("harness_session_id", sa.String(255), nullable=True),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_agent_events_event_type", "agent_events", ["event_type"])
    op.create_index("ix_agent_events_agent_id", "agent_events", ["agent_id"])
    op.create_index("ix_agent_events_work_key", "agent_events", ["work_key"])
    op.create_index("ix_agent_events_assignment_key", "agent_events", ["assignment_key"])
    op.create_index("ix_agent_events_correlation_id", "agent_events", ["correlation_id"])

    # --- session bindings (REQ-053) ---
    op.create_table(
        "agent_session_bindings",
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("session_title", sa.String(255), nullable=True),
        sa.Column("agent_running", sa.String(16), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_session_bindings")
    op.drop_index("ix_agent_events_correlation_id", table_name="agent_events")
    op.drop_index("ix_agent_events_assignment_key", table_name="agent_events")
    op.drop_index("ix_agent_events_work_key", table_name="agent_events")
    op.drop_index("ix_agent_events_agent_id", table_name="agent_events")
    op.drop_index("ix_agent_events_event_type", table_name="agent_events")
    op.drop_table("agent_events")
    op.drop_index("ix_agent_telemetry_samples_agent_id", table_name="agent_telemetry_samples")
    op.drop_table("agent_telemetry_samples")
    op.drop_table("agent_status_current")
    op.drop_table("acms_key_counters")
    op.drop_index("ix_work_assignments_assignment_key", table_name="work_assignments")
    op.drop_column("work_assignments", "bound_session_id")
    op.drop_column("work_assignments", "acknowledged_at")
    op.drop_column("work_assignments", "dispatched_at")
    op.drop_column("work_assignments", "assignment_key")
    op.drop_index("ix_work_items_work_key", table_name="work_items")
    op.drop_column("work_items", "work_key")
