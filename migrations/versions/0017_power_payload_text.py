"""Widen power_cost_snapshot.payload_json to hold the full facility payload.

STEA-004 Phase C live finding (2026-10-02): Release 5's fix stores channels +
rate + collector detail in payload_json (latest_power_summary rebuilds the §17
view from the STORED payload). The original 512-char cap truncated everything
but totals, so the stored-snapshot path rendered no per-channel cards. The
column becomes unbounded text; a 4KB application-side cap stays as a safety
net. Fully additive/nullable — no data rewritten.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_power_payload_text"
down_revision = "0016_power_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("power_cost_snapshot") as batch:
        batch.alter_column("payload_json", existing_type=sa.String(length=512),
                           type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    # Downgrade truncates stored payloads to 512 chars where needed (SQLite
    # batch recreates the table; PG ALTER back to varchar(512) requires the
    # data to fit — trim defensively first).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "UPDATE power_cost_snapshot "
            "SET payload_json = LEFT(payload_json, 512) "
            "WHERE payload_json IS NOT NULL AND LENGTH(payload_json) > 512"))
    with op.batch_alter_table("power_cost_snapshot") as batch:
        batch.alter_column("payload_json", existing_type=sa.Text(),
                           type_=sa.String(length=512), existing_nullable=True)