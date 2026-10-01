"""0013: agent durable-UID naming — legacy_name + worker_uid (STEA-004 §9).

Additive only (REQ-001: the agent_id UUID stays THE identity):
- agents.legacy_name  — the original/legacy display name kept as an alias
- agents.worker_uid   — the durable worker identifier (e.g. "001"), harness-agnostic
Backfills legacy_name = display_name for existing rows so the prior
`acms-worker-00N` names survive the display rename to
`acms-hermes-worker-uid-00N`.

Revision ID: 0013_agent_uid_naming
Revises: 0012_a2a_production_path
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_agent_uid_naming"
down_revision = "0012_a2a_production_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agents")}
    with op.batch_alter_table("agents") as batch:
        if "legacy_name" not in cols:
            batch.add_column(sa.Column("legacy_name", sa.String(length=255), nullable=True))
        if "worker_uid" not in cols:
            batch.add_column(sa.Column("worker_uid", sa.String(length=16), nullable=True))
    # backfill: legacy_name = current display_name for rows that have none
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE agents SET legacy_name = display_name WHERE legacy_name IS NULL")
    )
    ix = {i["name"] for i in sa.inspect(bind).get_indexes("agents")}
    if "ix_agents_worker_uid" not in ix:
        op.create_index("ix_agents_worker_uid", "agents", ["worker_uid"])


def downgrade() -> None:
    ix = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("agents")}
    if "ix_agents_worker_uid" in ix:
        op.drop_index("ix_agents_worker_uid", table_name="agents")
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agents")}
    with op.batch_alter_table("agents") as batch:
        if "worker_uid" in cols:
            batch.drop_column("worker_uid")
        if "legacy_name" in cols:
            batch.drop_column("legacy_name")
