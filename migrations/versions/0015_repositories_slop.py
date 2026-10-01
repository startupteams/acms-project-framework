"""0015: repository entities + Slop Ratio snapshots (STEA-004 plan §15/§16).

Additive tables only:
- repositories              durable repo entity (unique owner+repo)
- product_repository        Product↔Repository links (unique pair)
- project_repository        Project↔Repository links (unique pair)
- repository_metric_snapshot  LOC/ADR/ratio timeseries per §16

Revision ID: 0015_repositories_slop
Revises: 0014_work_artifact_uids
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_repositories_slop"
down_revision = "0014_work_artifact_uids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "repositories" not in tables:
        op.create_table(
            "repositories",
            sa.Column("repository_id", sa.String(length=36), primary_key=True),
            sa.Column("owner", sa.String(length=128), nullable=False),
            sa.Column("repo", sa.String(length=128), nullable=False),
            sa.Column("canonical_url", sa.String(length=512), nullable=True),
            sa.Column("default_branch", sa.String(length=128), nullable=True),
            sa.Column("last_commit_sha", sa.String(length=64), nullable=True),
            sa.Column("last_measured_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_loc", sa.Integer(), nullable=True),
            sa.Column("last_total_adr_count", sa.Integer(), nullable=True),
            sa.Column("last_human_adr_count", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("owner", "repo", name="uq_repositories_owner_repo"),
        )
    if "product_repository" not in tables:
        op.create_table(
            "product_repository",
            sa.Column("product_id", sa.String(length=36), primary_key=True),
            sa.Column("repository_id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("product_id", "repository_id", name="uq_product_repository"),
        )
    if "project_repository" not in tables:
        op.create_table(
            "project_repository",
            sa.Column("project_id", sa.String(length=36), primary_key=True),
            sa.Column("repository_id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "repository_id", name="uq_project_repository"),
        )
    if "repository_metric_snapshot" not in tables:
        op.create_table(
            "repository_metric_snapshot",
            sa.Column("snapshot_id", sa.String(length=36), primary_key=True),
            sa.Column("repository_id", sa.String(length=36), nullable=False, index=True),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("commit_sha", sa.String(length=64), nullable=True),
            sa.Column("loc", sa.Integer(), nullable=True),
            sa.Column("total_adr_count", sa.Integer(), nullable=True),
            sa.Column("human_written_adr_count", sa.Integer(), nullable=True),
            sa.Column("human_answer_count", sa.Integer(), nullable=True),
            sa.Column("true_slop_ratio", sa.Float(), nullable=True),
            sa.Column("precision_slop_ratio", sa.Float(), nullable=True),
            sa.Column("error", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for t in ("repository_metric_snapshot", "project_repository",
              "product_repository", "repositories"):
        if t in tables:
            op.drop_table(t)