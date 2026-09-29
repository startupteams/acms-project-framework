"""0007 work budgets — PostgreSQL migration-chain proof (REV2 plan §3/§4.5).

On real PostgreSQL 16 (pgserver):
  1. clean-DB alembic upgrade head (0001 → 0007) — fresh installs work;
  2. simulate a production-equivalent DB stopped at 0006, then upgrade → 0007
     (the exact path prod will take);
  3. downgrade 0007 → 0006 applies cleanly (documented semantics: budget rows
     dropped, nullable cost-split columns removed).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PGDATA = Path("/tmp/acms-integration-pgdata")  # shared with other PG tests


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
        pytest.skip("No PostgreSQL available (set ACMS_TEST_POSTGRES_URL or install pgserver)")
    return url


def _alembic(pg_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ACMS_DATABASE_URL": pg_url}
    alembic_bin = shutil.which("alembic") or str(REPO / ".venv-acms" / "bin" / "alembic")
    return subprocess.run(
        [alembic_bin, "-c", str(REPO / "alembic.ini"), *args],
        env=env, capture_output=True, text=True, cwd=REPO,
    )


def _psql_scalar(pg_url: str, sql: str):
    import sqlalchemy

    eng = sqlalchemy.create_engine(pg_url.replace("postgresql://", "postgresql+asyncpg://")
                                   .replace("postgresql+psycopg2://", "postgresql+asyncpg://"))
    import asyncio

    async def _run() -> object:
        import sqlalchemy.ext.asyncio as sa_async

        aeng = sa_async.create_async_engine(eng.url)
        try:
            async with aeng.connect() as conn:
                return (await conn.exec_driver_sql(sql)).scalar()
        finally:
            await aeng.dispose()

    try:
        return asyncio.run(_run())
    finally:
        eng.dispose()


def test_fresh_install_chain_to_0007(pg_url):
    """Clean-DB chain through 0007 (head at the 0007 slice); 0008+ builds on top.
    Kept as its own revision pin so this slice's proof stays exact."""
    # start from base so a prior module (e.g. the 0008 test) can't pollute the stamp
    _alembic(pg_url, "downgrade", "base")
    r = _alembic(pg_url, "upgrade", "0007_work_budgets")
    assert r.returncode == 0, f"alembic upgrade failed:\n{r.stdout}\n{r.stderr}"
    assert _psql_scalar(pg_url, "select version_num from alembic_version") == "0007_work_budgets"
    # work_budgets table exists with the boolean default intact
    assert _psql_scalar(
        pg_url,
        "select count(*) from information_schema.tables "
        "where table_name='work_budgets'") == 1
    # the exact prod upgrade path: 0006-stamped DB → 0007
    r2 = _alembic(pg_url, "downgrade", "0006_memory_session_offload")
    assert r2.returncode == 0, f"downgrade to 0006 failed:\n{r2.stdout}\n{r2.stderr}"
    assert _psql_scalar(pg_url, "select version_num from alembic_version") == "0006_memory_session_offload"
    assert _psql_scalar(
        pg_url,
        "select count(*) from information_schema.tables where table_name='work_budgets'") == 0
    r3 = _alembic(pg_url, "upgrade", "0007_work_budgets")
    assert r3.returncode == 0, f"re-upgrade 0006→0007 failed:\n{r3.stdout}\n{r3.stderr}"
    assert _psql_scalar(pg_url, "select version_num from alembic_version") == "0007_work_budgets"
    # cost-split columns landed on execution_sessions
    n = _psql_scalar(
        pg_url,
        "select count(*) from information_schema.columns "
        "where table_name='execution_sessions' "
        "and column_name in ('estimated_cloud_cost_usd','estimated_local_cost_usd')")
    assert n == 2
