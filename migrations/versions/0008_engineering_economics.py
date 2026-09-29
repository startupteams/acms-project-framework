"""0008 engineering economics — outcome correlation (REV2 plan §9B).

New tables:
  pr_outcomes — one row per PR/branch execution outcome; explicit acceptance
    states where ACCEPTED stays nullable/unknown until a human/product
    acceptance event exists (never fabricated from "merged").
  requirement_links — many-to-many PR ↔ requirement correlation (one PR may
    close several requirements; one requirement may span several PRs).
  cost_attribution — per-execution cost records correlated to a PR outcome
    (session/aggregated rows; failed/retried runs keep their cost).

Extension:
  execution_sessions.task_category — lightweight task classification tag
    (architecture|backend|frontend|database|infrastructure|debugging|testing|
    documentation|review|research) for model-economics comparison by task type.

Revision ID: 0008_engineering_economics
Revises: 0007_work_budgets
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_engineering_economics"
down_revision = "0007_work_budgets"
branch_labels = None
depends_on = None

TASK_CATEGORIES = (
    "architecture", "backend", "frontend", "database", "infrastructure",
    "debugging", "testing", "documentation", "review", "research",
)


def upgrade() -> None:
    op.create_table(
        "pr_outcomes",
        sa.Column("outcome_id", sa.String(36), primary_key=True),
        sa.Column("work_item_id", sa.String(36), nullable=True, index=True),
        sa.Column("agent_id", sa.String(36), nullable=True, index=True),
        sa.Column("repository", sa.String(255), nullable=True),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(512), nullable=True),
        # renamed commit_shas → commit_shas_json in 0009 (ORM parity; fresh
        # installs create the final name directly)
        sa.Column("commit_shas_json", sa.Text(), nullable=True),  # JSON list
        # OPEN|MERGED|CI_VERIFIED|DEPLOYED|LIVE_VERIFIED|ACCEPTED|REWORK_REQUIRED|REVERTED
        sa.Column("outcome_state", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.String(128), nullable=True),
        sa.Column("model_id", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("task_category", sa.String(24), nullable=True),
        sa.Column("session_count", sa.Integer(), nullable=True),
        sa.Column("checkpoint_count", sa.Integer(), nullable=True),
        sa.Column("human_review_minutes", sa.Float(), nullable=True),
        sa.Column("human_repair_minutes", sa.Float(), nullable=True),
        sa.Column("wall_clock_seconds", sa.Integer(), nullable=True),
        sa.Column("first_pass", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pr_outcomes_repo_pr", "pr_outcomes", ["repository", "pr_number"])
    op.create_index("ix_pr_outcomes_state", "pr_outcomes", ["outcome_state"])

    op.create_table(
        "requirement_links",
        sa.Column("link_id", sa.String(36), primary_key=True),
        sa.Column("outcome_id", sa.String(36),
                  sa.ForeignKey("pr_outcomes.outcome_id"), index=True),
        sa.Column("requirement_ref", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("outcome_id", "requirement_ref"),
    )

    op.create_table(
        "cost_attribution",
        sa.Column("entry_id", sa.String(36), primary_key=True),
        sa.Column("outcome_id", sa.String(36),
                  sa.ForeignKey("pr_outcomes.outcome_id"), index=True),
        sa.Column("session_id", sa.String(36), nullable=True, index=True),
        sa.Column("model_id", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("api_cost_usd", sa.Float(), nullable=True),
        # local usage recorded, USD-equivalent stays unknown (no authoritative source)
        sa.Column("local_compute_seconds", sa.Float(), nullable=True),
        sa.Column("local_cost_usd", sa.Float(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="session"),
        sa.Column("failed_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cost_attribution_outcome_model", "cost_attribution",
                    ["outcome_id", "model_id"])

    op.add_column("execution_sessions",
                  sa.Column("task_category", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_sessions", "task_category")
    op.drop_table("cost_attribution")
    op.drop_table("requirement_links")
    op.drop_table("pr_outcomes")
