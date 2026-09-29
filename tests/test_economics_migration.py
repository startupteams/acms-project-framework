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
    r = _alembic(pg_url, "upgrade", "head")
    assert r.returncode == 0, f"upgrade head failed:\n{r.stdout}\n{r.stderr}"
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
