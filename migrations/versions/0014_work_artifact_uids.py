"""0014: human-readable Work/Artifact UIDs (STEA-004 plan §6/§7).

Additive only — internal UUIDs remain THE identity (REQ-001, plan §47 stop
condition: never rewrite UUID identity):

- work_items.work_uid        ACMS-WORK-######-YYYYMMDD_HHMMSS (unique)
- work_items.work_sequence   monotonic sequence backing the UID
- artifacts.artifact_uid     ACMS-ARTIFACT-######-YYYYMMDD_HHMMSS (unique)
- artifacts.artifact_sequence monotonic sequence backing the UID

Counter rows seeded into acms_key_counters from the max existing sequence so
post-migration allocation never collides with backfilled values.

Backfill is deterministic per plan §6: work items by (created_at ASC, id ASC),
artifacts by (created_at ASC, artifact_id ASC). Work items whose work_key
already encodes their sequence (REQ-052) KEEP that number — the long UID is
derived from the same sequence so short key and UID stay the same row-number;
rows without a work_key get fresh sequence numbers after the existing max.
Artifacts are numbered 1..N from oldest to newest.

SQLite AND PostgreSQL both supported (no RETURNING tricks, portable SQL).
Revision ID: 0014_work_artifact_uids
Revises: 0013_agent_uid_naming
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_work_artifact_uids"
down_revision = "0013_agent_uid_naming"
branch_labels = None
depends_on = None


def _ts(created_at) -> str:
    from datetime import datetime, timezone

    if created_at is None:
        return "19700101_000000"
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError:
            return "19700101_000000"
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


def upgrade() -> None:
    bind = op.get_bind()

    # ---- additive columns ---------------------------------------------------
    wcols = {c["name"] for c in sa.inspect(bind).get_columns("work_items")}
    with op.batch_alter_table("work_items") as batch:
        if "work_uid" not in wcols:
            batch.add_column(sa.Column("work_uid", sa.String(length=64), nullable=True))
        if "work_sequence" not in wcols:
            batch.add_column(sa.Column("work_sequence", sa.Integer(), nullable=True))
    acols = {c["name"] for c in sa.inspect(bind).get_columns("artifacts")}
    with op.batch_alter_table("artifacts") as batch:
        if "artifact_uid" not in acols:
            batch.add_column(sa.Column("artifact_uid", sa.String(length=64), nullable=True))
        if "artifact_sequence" not in acols:
            batch.add_column(sa.Column("artifact_sequence", sa.Integer(), nullable=True))

    # ---- unique indexes (created after backfill; rows are unique by design) -
    widx = {i["name"] for i in sa.inspect(bind).get_indexes("work_items")}
    if "ix_work_items_work_uid" not in widx:
        op.create_index("ix_work_items_work_uid", "work_items", ["work_uid"], unique=True)
    aidx = {i["name"] for i in sa.inspect(bind).get_indexes("artifacts")}
    if "ix_artifacts_artifact_uid" not in aidx:
        op.create_index("ix_artifacts_artifact_uid", "artifacts", ["artifact_uid"], unique=True)

    # ---- work item backfill --------------------------------------------------
    # Rows WITH a work_key: keep that sequence number (short key stays row-true).
    # Rows WITHOUT: allocate after the max of the above.
    work_rows = bind.execute(sa.text(
        "SELECT work_item_id, work_key, created_at FROM work_items ORDER BY created_at ASC, work_item_id ASC"
    )).fetchall()
    max_work_seq = 0
    for rid, key, created_at in work_rows:
        if key and key.startswith("ACMS-WORK-") and key[len("ACMS-WORK-"):].isdigit():
            seq = int(key[len("ACMS-WORK-"):])
        else:
            seq = None
        if seq is not None:
            max_work_seq = max(max_work_seq, seq)
    next_seq = max_work_seq
    for rid, key, created_at in work_rows:
        if key and key.startswith("ACMS-WORK-") and key[len("ACMS-WORK-"):].isdigit():
            seq = int(key[len("ACMS-WORK-"):])
        else:
            next_seq += 1
            seq = next_seq
        uid = f"ACMS-WORK-{seq:06d}-{_ts(created_at)}"
        bind.execute(sa.text(
            "UPDATE work_items SET work_uid = :uid, work_sequence = :seq WHERE work_item_id = :rid"
        ), {"uid": uid, "seq": seq, "rid": rid})

    # ---- artifact backfill ---------------------------------------------------
    art_rows = bind.execute(sa.text(
        "SELECT artifact_id, created_at FROM artifacts ORDER BY created_at ASC, artifact_id ASC"
    )).fetchall()
    for i, (rid, created_at) in enumerate(art_rows, start=1):
        uid = f"ACMS-ARTIFACT-{i:06d}-{_ts(created_at)}"
        bind.execute(sa.text(
            "UPDATE artifacts SET artifact_uid = :uid, artifact_sequence = :seq WHERE artifact_id = :rid"
        ), {"uid": uid, "seq": i, "rid": rid})

    # ---- counter seeding -----------------------------------------------------
    # acms_key_counters may not exist yet on very old DBs (0003 created it);
    # guard with a portable existence check.
    tables = set(sa.inspect(bind).get_table_names())
    if "acms_key_counters" in tables:
        bind.execute(sa.text(
            "INSERT INTO acms_key_counters (counter_name, counter_value) "
            "SELECT 'work_key', :w WHERE :w > COALESCE((SELECT counter_value FROM "
            "acms_key_counters WHERE counter_name = 'work_key'), 0) "
            "AND NOT EXISTS (SELECT 1 FROM acms_key_counters WHERE counter_name = 'work_key')"
        ), {"w": next_seq})
        # If the counter row exists but is lower than backfilled max, raise it.
        bind.execute(sa.text(
            "UPDATE acms_key_counters SET counter_value = :w "
            "WHERE counter_name = 'work_key' AND counter_value < :w"
        ), {"w": next_seq})
        max_art_seq = len(art_rows)
        bind.execute(sa.text(
            "INSERT INTO acms_key_counters (counter_name, counter_value) "
            "SELECT 'artifact_uid', :a WHERE NOT EXISTS "
            "(SELECT 1 FROM acms_key_counters WHERE counter_name = 'artifact_uid')"
        ), {"a": max_art_seq})
        bind.execute(sa.text(
            "UPDATE acms_key_counters SET counter_value = :a "
            "WHERE counter_name = 'artifact_uid' AND counter_value < :a"
        ), {"a": max_art_seq})


def downgrade() -> None:
    aidx = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("artifacts")}
    if "ix_artifacts_artifact_uid" in aidx:
        op.drop_index("ix_artifacts_artifact_uid", table_name="artifacts")
    widx = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("work_items")}
    if "ix_work_items_work_uid" in widx:
        op.drop_index("ix_work_items_work_uid", table_name="work_items")
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("artifacts")}
    with op.batch_alter_table("artifacts") as batch:
        if "artifact_sequence" in cols:
            batch.drop_column("artifact_sequence")
        if "artifact_uid" in cols:
            batch.drop_column("artifact_uid")
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("work_items")}
    with op.batch_alter_table("work_items") as batch:
        if "work_sequence" in cols:
            batch.drop_column("work_sequence")
        if "work_uid" in cols:
            batch.drop_column("work_uid")