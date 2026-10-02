"""Migration 0017 chain test — power_cost_snapshot.payload_json widened to Text.

SQLite full upgrade→downgrade cycle on a SEPARATE scratch DB (the shared unit
DB is create_all-managed; alembic needs an empty schema). PG semantics covered
by the parity test.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def alembic_env(tmp_path, monkeypatch):
    """Fresh alembic config against a scratch SYNC SQLite file (aiosqlite's
    cross-engine locking makes sync verification flaky on async URLs)."""
    from alembic import config
    import os

    dbpath = tmp_path / "mig-test.db"
    url = f"sqlite:///{dbpath}"
    monkeypatch.setenv("ACMS_DATABASE_URL", url)
    cfg = config.Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("acms_sync_url", url)
    return cfg


def test_0017_upgrade_downgrade_sqlite(alembic_env):
    """Full chain upgrade to head then back to base — SQLite batch must survive."""
    from alembic import command
    from sqlalchemy import create_engine, inspect

    url = alembic_env.get_main_option("sqlalchemy.url")
    command.upgrade(alembic_env, "head")
    eng = create_engine(url)
    cols = {c["name"] for c in inspect(eng).get_columns("power_cost_snapshot")}
    assert "payload_json" in cols
    command.downgrade(alembic_env, "0016_power_snapshot")
    command.downgrade(alembic_env, "base")
    command.upgrade(alembic_env, "head")


def test_0017_accepts_long_payload(alembic_env, tmp_path):
    """The whole point: a >512-char payload must round-trip (PG regression class)."""
    from alembic import command
    from sqlalchemy import create_engine, text

    command.upgrade(alembic_env, "head")
    # env.py rewrites sqlalchemy.url to the ASYNC form; use the raw path
    eng = create_engine(f"sqlite:///{tmp_path}/mig-test.db")
    long_payload = "x" * 3000
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO power_cost_snapshot (snapshot_id, captured_at, source, payload_json) "
            "VALUES ('snap-test', CURRENT_TIMESTAMP, 'test', :p)"), {"p": long_payload})
        row = conn.execute(text(
            "SELECT payload_json FROM power_cost_snapshot WHERE snapshot_id='snap-test'")).scalar()
    assert row == long_payload
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM power_cost_snapshot WHERE snapshot_id='snap-test'"))