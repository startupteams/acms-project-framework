"""0014 migration backfill determinism (STEA-004 plan §6/§7).

Runs the REAL 0014 migration on a fresh SQLite DB with pre-seeded rows
(mixed work_key present/absent, out-of-order ids) and asserts:
- deterministic order (created_at ASC, id ASC)
- work rows with work_key keep that sequence number
- artifacts numbered 1..N oldest-first
- counter rows seeded past backfilled max (post-migration allocation collides
  with nothing)
- downgrade is clean

Uses ACMS_DATABASE_URL env routing (migrations/env.py contract).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture()
def _migrated_db(tmp_path, monkeypatch):
    from alembic.config import Config
    from alembic import command

    url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setenv("ACMS_DATABASE_URL", url)
    cfg = Config("alembic.ini")

    command.upgrade(cfg, "0013_agent_uid_naming")

    import sqlite3

    db = sqlite3.connect(tmp_path / "mig.db")
    now = "2026-10-01 12:00:00"
    db.execute(
        "INSERT INTO work_items (work_item_id, kind, title, status, scope_markdown, "
        "created_at, updated_at, work_key) VALUES "
        "('w-with-key', 'task', 'has key', 'PLANNED', '', ?, ?, 'ACMS-WORK-000042')", (now, now))
    db.execute(
        "INSERT INTO work_items (work_item_id, kind, title, status, scope_markdown, "
        "created_at, updated_at) VALUES "
        "('w-no-key-later', 'task', 'no key later', 'PLANNED', '', '2026-10-01 13:00:00', ?)", (now,))
    db.execute(
        "INSERT INTO work_items (work_item_id, kind, title, status, scope_markdown, "
        "created_at, updated_at) VALUES "
        "('w-no-key-earlier', 'task', 'no key earlier', 'PLANNED', '', '2026-10-01 11:00:00', ?)", (now,))
    for aid, t in [("a-old", "2026-10-01 10:00:00"),
                   ("a-new", "2026-10-01 14:00:00")]:
        db.execute(
            "INSERT INTO artifacts (artifact_id, artifact_type, title, content, sha256, "
            "created_at) VALUES (?, 'handoff', ?, 'x', 'sha', ?)", (aid, t, t))
    db.commit()

    command.upgrade(cfg, "0014_work_artifact_uids")
    yield db
    db.close()


def test_0014_backfill_determinism(_migrated_db):
    db = _migrated_db
    rows = {r[0]: (r[1], r[2]) for r in
            db.execute("SELECT work_item_id, work_uid, work_sequence FROM work_items").fetchall()}
    # work_key row keeps its number (42)
    assert rows["w-with-key"][0].startswith("ACMS-WORK-000042-")
    assert rows["w-with-key"][1] == 42
    # keyless rows: earlier gets 43, later gets 44 (created_at order)
    assert rows["w-no-key-earlier"][1] == 43
    assert rows["w-no-key-later"][1] == 44

    arts = {r[0]: (r[1], r[2]) for r in
            db.execute("SELECT artifact_id, artifact_uid, artifact_sequence FROM artifacts").fetchall()}
    assert arts["a-old"][1] == 1  # oldest artifact = 1
    assert arts["a-new"][1] == 2
    assert arts["a-old"][0].startswith("ACMS-ARTIFACT-000001-")
    assert arts["a-new"][0].startswith("ACMS-ARTIFACT-000002-")

    # counter seeded past max so next allocation cannot collide
    wk = db.execute("SELECT counter_value FROM acms_key_counters WHERE counter_name='work_key'").fetchone()[0]
    au = db.execute("SELECT counter_value FROM acms_key_counters WHERE counter_name='artifact_uid'").fetchone()[0]
    assert wk >= 44
    assert au >= 2


def test_0014_downgrade_clean(tmp_path, monkeypatch):
    from alembic.config import Config
    from alembic import command

    url = f"sqlite:///{tmp_path}/mig2.db"
    monkeypatch.setenv("ACMS_DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0014_work_artifact_uids")
    command.downgrade(cfg, "0013_agent_uid_naming")
    import sqlite3

    db = sqlite3.connect(tmp_path / "mig2.db")
    cols_w = {r[1] for r in db.execute("PRAGMA table_info(work_items)")}
    cols_a = {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
    assert "work_uid" not in cols_w and "work_sequence" not in cols_w
    assert "artifact_uid" not in cols_a and "artifact_sequence" not in cols_a