"""0009 pr_outcomes.commit_shas → commit_shas_json (ORM-parity repair).

Window-4 live finding: migration 0008 created the column as ``commit_shas``
while the ORM model + economics ingest/service code expect
``commit_shas_json``. Any economics write against a migrated production DB
failed with ``column pr_outcomes.commit_shas_json does not exist`` (SQLite
unit tests never caught it — create_all builds from the model, not the
migration). Production ``pr_outcomes`` held ZERO rows at repair time (the
ingest backfill had never succeeded), so a plain RENAME is lossless.

Idempotent by column-existence probe:
  - DBs migrated from 0008 (column ``commit_shas``) → renamed.
  - Fresh installs (0008 fixed to create ``commit_shas_json``) → no-op.
  - Already-repaired DBs (0009 applied twice is impossible, but re-runs of
    the chain from backups) → no-op.

Revision ID: 0009_pr_outcome_column_parity
Revises: 0008_engineering_economics
Create Date: 2026-09-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_pr_outcome_column_parity"
down_revision = "0008_engineering_economics"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    cols = _column_names("pr_outcomes")
    if "commit_shas" in cols and "commit_shas_json" not in cols:
        with op.batch_alter_table("pr_outcomes") as batch:
            batch.alter_column(
                "commit_shas",
                new_column_name="commit_shas_json",
                existing_type=sa.Text(),
                existing_nullable=True,
            )
    # else: fresh install (0008 already creates commit_shas_json) or already
    # repaired — no-op.


def downgrade() -> None:
    cols = _column_names("pr_outcomes")
    if "commit_shas_json" in cols and "commit_shas" not in cols:
        with op.batch_alter_table("pr_outcomes") as batch:
            batch.alter_column(
                "commit_shas_json",
                new_column_name="commit_shas",
                existing_type=sa.Text(),
                existing_nullable=True,
            )
