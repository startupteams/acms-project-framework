"""provisioning_requests.attempt (JINT-001 retry semantics)

Revision ID: 0005_prov_attempt
Revises: 0004_provisioning_requests
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0005_prov_attempt"
down_revision = "0004_provisioning_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provisioning_requests",
                  sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("provisioning_requests", "attempt")
