"""0010: Jira kickoff linkage + reconciliation tables (window-5 plan §7/§13).

Adds:
- work_items.jira_* columns (linkage + last observation + eligibility verdict)
- jira_issue_observations (append-only reads)
- work_runtime_holds (persistent local pause/stop; survive restarts and polls)
- jira_reconciliation_runs (durable run ledger + lease)
- jira_issue_links ((site_id, issue_id) ↔ top-level work authorization)
- jira_scheduler_state (single-row scheduler bookkeeping, UTC)

Additive only — no data is rewritten. SQLite-compatible (unit tests) AND
PostgreSQL-checked via the parity/chain tests.

Revision ID: 0010_jira_kickoff_gate
Revises: 0009_pr_outcome_column_parity
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_jira_kickoff_gate"
down_revision = "0009_pr_outcome_column_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("work_items")}
    jira_cols = [
        sa.Column("jira_site_id", sa.String(length=128), nullable=True),
        sa.Column("jira_issue_id", sa.String(length=64), nullable=True),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("jira_url", sa.String(length=512), nullable=True),
        sa.Column("jira_last_status", sa.String(length=64), nullable=True),
        sa.Column("jira_last_assignee_account_id", sa.String(length=128), nullable=True),
        sa.Column("jira_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("jira_eligibility", sa.String(length=32), nullable=True),
        sa.Column("jira_eligibility_reason", sa.String(length=512), nullable=True),
    ]
    with op.batch_alter_table("work_items") as batch:
        for c in jira_cols:
            if c.name not in cols:
                batch.add_column(c)

    op.create_table(
        "jira_issue_observations",
        sa.Column("observation_id", sa.String(length=36), primary_key=True),
        sa.Column("work_item_id", sa.String(length=36),
                  sa.ForeignKey("work_items.work_item_id", ondelete="SET NULL"), nullable=True),
        sa.Column("jira_site_id", sa.String(length=128), nullable=True),
        sa.Column("jira_issue_id", sa.String(length=64), nullable=False),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("observed_status", sa.String(length=64), nullable=True),
        sa.Column("observed_assignee_account_id", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual_check"),
        sa.Column("read_outcome", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("detail_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_jira_obs_issue_id", "jira_issue_observations", ["jira_issue_id"])
    op.create_index("ix_jira_obs_work_item", "jira_issue_observations", ["work_item_id"])

    op.create_table(
        "work_runtime_holds",
        sa.Column("hold_id", sa.String(length=36), primary_key=True),
        sa.Column("work_item_id", sa.String(length=36),
                  sa.ForeignKey("work_items.work_item_id", ondelete="CASCADE"), nullable=False),
        sa.Column("hold_kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="ui"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.String(length=128), nullable=True),
        sa.Column("runtime_acknowledged", sa.Boolean(), nullable=True),
        sa.Column("ack_detail_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_holds_work_item", "work_runtime_holds", ["work_item_id"])
    op.create_index("ix_holds_cleared_at", "work_runtime_holds", ["cleared_at"])

    op.create_table(
        "jira_reconciliation_runs",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("examined_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("added_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resumed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_acknowledged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stopped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stopped_acknowledged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scan_completeness", sa.String(length=16), nullable=False, server_default="full"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recon_runs_created", "jira_reconciliation_runs", ["created_at"])

    op.create_table(
        "jira_issue_links",
        sa.Column("link_id", sa.String(length=36), primary_key=True),
        sa.Column("jira_site_id", sa.String(length=128), nullable=False, server_default="default"),
        sa.Column("jira_issue_id", sa.String(length=64), nullable=False),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("jira_url", sa.String(length=512), nullable=True),
        sa.Column("work_item_id", sa.String(length=36),
                  sa.ForeignKey("work_items.work_item_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_ready_observation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_generation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("jira_site_id", "jira_issue_id", name="uq_jira_link_site_issue"),
    )
    op.create_index("ix_links_work_item", "jira_issue_links", ["work_item_id"])

    op.create_table(
        "jira_scheduler_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_successful_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.String(length=36), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("jira_scheduler_state")
    op.drop_index("ix_links_work_item", table_name="jira_issue_links")
    op.drop_table("jira_issue_links")
    op.drop_index("ix_recon_runs_created", table_name="jira_reconciliation_runs")
    op.drop_table("jira_reconciliation_runs")
    op.drop_index("ix_holds_cleared_at", table_name="work_runtime_holds")
    op.drop_index("ix_holds_work_item", table_name="work_runtime_holds")
    op.drop_table("work_runtime_holds")
    op.drop_index("ix_jira_obs_work_item", table_name="jira_issue_observations")
    op.drop_index("ix_jira_obs_issue_id", table_name="jira_issue_observations")
    op.drop_table("jira_issue_observations")
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("work_items")}
    with op.batch_alter_table("work_items") as batch:
        for name in ("jira_site_id", "jira_issue_id", "jira_issue_key", "jira_url",
                     "jira_last_status", "jira_last_assignee_account_id",
                     "jira_last_checked_at", "jira_eligibility", "jira_eligibility_reason"):
            if name in cols:
                batch.drop_column(name)