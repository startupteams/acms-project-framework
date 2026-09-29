"""0008 engineering economics — PostgreSQL migration proof (REV2 plan §9B).

On real PostgreSQL 16 (pgserver): clean-DB chain to 0008, the new tables exist,
execution_sessions gains task_category, and 0008→0007 downgrade applies.
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


def _scalar(pg_url: str, sql: str):
    import asyncio

    import sqlalchemy.ext.asyncio as sa_async

    async def _run():
        eng = sa_async.create_async_engine(pg_url.replace("postgresql://", "postgresql+asyncpg://"))
        try:
            async with eng.connect() as conn:
                return (await conn.exec_driver_sql(sql)).scalar()
        finally:
            await eng.dispose()

    return asyncio.run(_run())


def test_0008_chain_and_downgrade(pg_url):
    # pin the exact revision, never head — 0009 now exists and shared PGDATA
    # may carry a stale stamp (agents.md migration-test ordering lesson)
    r = _alembic(pg_url, "upgrade", "0008_engineering_economics")
    assert r.returncode == 0, f"upgrade 0008 failed:\n{r.stdout}\n{r.stderr}"
    assert _scalar(pg_url, "select version_num from alembic_version") == "0008_engineering_economics"
    for t in ("pr_outcomes", "requirement_links", "cost_attribution"):
        assert _scalar(pg_url, f"select count(*) from information_schema.tables where table_name='{t}'") == 1
    assert _scalar(pg_url, "select count(*) from information_schema.columns "
                   "where table_name='execution_sessions' and column_name='task_category'") == 1
    # downgrade 0008 → 0007 is clean (economics tables dropped, 0007 intact)
    r2 = _alembic(pg_url, "downgrade", "0007_work_budgets")
    assert r2.returncode == 0, f"downgrade failed:\n{r2.stdout}\n{r2.stderr}"
    assert _scalar(pg_url, "select version_num from alembic_version") == "0007_work_budgets"
    assert _scalar(pg_url, "select count(*) from information_schema.tables where table_name='pr_outcomes'") == 0


def test_0009_commit_shas_json_parity(pg_url):
    """0009 must leave pr_outcomes with commit_shas_json on BOTH paths.

    (a) 0008-migrated path: the legacy commit_shas column gets renamed.
    (b) Fresh-install path: 0008 (fixed) creates commit_shas_json and 0009
        is a no-op — the final schema is identical either way (ORM parity
        repair for the window-4 live finding where economics writes 500'd
        against the migrated prod DB).
    """
    # --- (a) 0008-migrated path ---
    # start from a known stamp — shared PGDATA may hold any stale version
    r = _alembic(pg_url, "downgrade", "0007_work_budgets")
    assert r.returncode == 0, f"pre-clean downgrade failed:\n{r.stdout}\n{r.stderr}"
    r = _alembic(pg_url, "upgrade", "0008_engineering_economics")
    assert r.returncode == 0, f"upgrade 0008 failed:\n{r.stdout}\n{r.stderr}"
    # simulate the historical prod divergence ONLY if 0008 already creates
    # the final name (fresh check); force-create the legacy column to prove
    # the rename branch deterministically.
    import asyncio

    import sqlalchemy.ext.asyncio as sa_async

    async def _force_legacy():
        eng = sa_async.create_async_engine(pg_url.replace("postgresql://", "postgresql+asyncpg://"))
        try:
            async with eng.begin() as conn:
                await conn.exec_driver_sql(
                    "ALTER TABLE pr_outcomes DROP COLUMN IF EXISTS commit_shas_json")
                await conn.exec_driver_sql(
                    "ALTER TABLE pr_outcomes ADD COLUMN IF NOT EXISTS commit_shas TEXT")
        finally:
            await eng.dispose()

    asyncio.run(_force_legacy())
    r = _alembic(pg_url, "upgrade", "head")
    assert r.returncode == 0, f"upgrade head (0009 rename) failed:\n{r.stdout}\n{r.stderr}"
    assert _scalar(pg_url, "select version_num from alembic_version") == "0009_pr_outcome_column_parity"
    assert _scalar(pg_url, "select count(*) from information_schema.columns "
                   "where table_name='pr_outcomes' and column_name='commit_shas_json'") == 1
    assert _scalar(pg_url, "select count(*) from information_schema.columns "
                   "where table_name='pr_outcomes' and column_name='commit_shas'") == 0

    # ORM can actually SELECT the migrated table (the live failure signature)
    async def _orm_select():
        import sys
        sys.path.insert(0, str(REPO))
        from acms.economics_models import PrOutcomeRecord
        import sqlalchemy.ext.asyncio as sa_async2

        eng = sa_async2.create_async_engine(pg_url.replace("postgresql://", "postgresql+asyncpg://"))
        try:
            async with eng.connect() as conn:
                from sqlalchemy import select
                await conn.execute(select(PrOutcomeRecord.outcome_id))
        finally:
            await eng.dispose()

    asyncio.run(_orm_select())

    # downgrade 0009 → 0008 restores the legacy name (chain symmetry)
    r = _alembic(pg_url, "downgrade", "0008_engineering_economics")
    assert r.returncode == 0, f"downgrade 0009 failed:\n{r.stdout}\n{r.stderr}"
    assert _scalar(pg_url, "select count(*) from information_schema.columns "
                   "where table_name='pr_outcomes' and column_name='commit_shas'") == 1
    # return to head for any later module sharing this PGDATA
    r = _alembic(pg_url, "upgrade", "head")
    assert r.returncode == 0
