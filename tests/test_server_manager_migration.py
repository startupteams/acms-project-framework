"""JINT-001 — verify the 0004 migration applies cleanly on PostgreSQL (pgserver pattern from test_postgres_integration)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PGDATA = Path("/tmp/acms-integration-pgdata")  # shared with test_postgres_integration


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


def test_0004_provisioning_requests_applies(pg_url):
    env = {**os.environ, "ACMS_DATABASE_URL": pg_url}
    r = subprocess.run(
        [f"{REPO}/.venv-acms/bin/alembic", "-c", str(REPO / "alembic.ini"),
         "upgrade", "head"],
        env=env, capture_output=True, text=True, cwd=REPO,
    )
    assert r.returncode == 0, f"alembic upgrade failed:\n{r.stdout}\n{r.stderr}"