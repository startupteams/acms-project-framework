"""Migration-vs-model parity regression (window-4 Phase J).

The 0008 incident class: migration created ``commit_shas`` while the ORM model
expected ``commit_shas_json`` — SQLite unit tests never catch it because
create_all builds from MODELS. This test upgrades a real PostgreSQL (pgserver)
through the FULL migration chain, then asserts every model-mapped table's
column set matches what the ORM declares. Any future model/migration drift
fails here BEFORE production.

Runs on the shared /tmp/acms-integration-pgdata PGDATA used by
test_budget_migration.py — pin revisions and downgrade base first (ordering
lesson, agents.md).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PGDATA = Path("/tmp/acms-integration-pgdata")


def _resolve_pg_url() -> str | None:
    url = os.environ.get("ACMS_TEST_POSTGRES_URL")
    if url:
        return url
    try:
        import pgserver

        return pgserver.get_server(PGDATA, cleanup_mode="delete").get_uri()
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_url() -> str:
    url = _resolve_pg_url()
    if url is None:
        pytest.skip("No PostgreSQL available")
    return url


def _alembic(pg_url: str, *args: str):
    env = {**os.environ, "ACMS_DATABASE_URL": pg_url}
    alembic_bin = shutil.which("alembic") or str(REPO / ".venv-acms" / "bin" / "alembic")
    return subprocess.run(
        [alembic_bin, "-c", str(REPO / "alembic.ini"), *args],
        env=env, capture_output=True, text=True, cwd=REPO,
    )


def test_migration_chain_matches_orm_models(pg_url):
    """head-migrate a real PG, then diff every ORM table's columns vs actual."""
    # deterministic stamp (shared PGDATA ordering lesson)
    r = _alembic(pg_url, "downgrade", "base")
    assert r.returncode == 0, f"downgrade base failed:\n{r.stdout}\n{r.stderr}"
    r = _alembic(pg_url, "upgrade", "head")
    assert r.returncode == 0, f"upgrade head failed:\n{r.stdout}\n{r.stderr}"

    import asyncio

    import sqlalchemy.ext.asyncio as sa_async

    async def _collect():
        import sys
        sys.path.insert(0, str(REPO))
        from acms.db import Base
        import acms.work_models  # noqa: F401
        import acms.telemetry_models  # noqa: F401
        import acms.memory_models  # noqa: F401
        import acms.economics_models  # noqa: F401
        import acms.jira_models  # noqa: F401  (jira kickoff/reconciliation, window-5)
        import acms.models  # noqa: F401  (agent registry)

        eng = sa_async.create_async_engine(pg_url.replace("postgresql://", "postgresql+asyncpg://"))
        drifts = []
        try:
            async with eng.connect() as conn:
                from sqlalchemy import inspect

                def _sync_inspect(conn_sync):
                    insp = inspect(conn_sync)
                    for table in Base.metadata.sorted_tables:
                        actual = {c["name"] for c in insp.get_columns(table.name)}
                        expected = {c.name for c in table.columns}
                        if actual != expected:
                            drifts.append({
                                "table": table.name,
                                "in_migration_not_model": sorted(actual - expected),
                                "in_model_not_migration": sorted(expected - actual),
                            })
                await conn.run_sync(_sync_inspect)
        finally:
            await eng.dispose()
        return drifts

    drifts = asyncio.run(_collect())
    assert drifts == [], (
        "Migration chain vs ORM model drift detected (the 0008 commit_shas class): "
        f"{drifts}"
    )
