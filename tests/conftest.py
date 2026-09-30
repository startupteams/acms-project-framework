import asyncio
import os
from pathlib import Path

import pytest

# Isolated SQLite file for async unit tests (aiosqlite driver is applied in acms.db).
# PostgreSQL integration tests use a real PostgreSQL 16 server (tests/test_postgres_integration.py).
TEST_DB = Path("/tmp/acms-unit.db")
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["ACMS_ADMIN_TOKEN"] = "test-token"
os.environ["ACMS_DATABASE_URL"] = f"sqlite:///{TEST_DB}"

from acms.db import Base, engine  # noqa: E402
from acms import models  # noqa: E402,F401  (register tables on Base.metadata)
from acms import work_models  # noqa: E402,F401  (register work tables on Base.metadata)
from acms import telemetry_models  # noqa: E402,F401  (register telemetry tables on Base.metadata)
from acms import server_manager_api  # noqa: E402,F401  (register provisioning_requests on Base.metadata)
from acms import work_keys  # noqa: E402,F401  (register acms_key_counters on Base.metadata)
from acms import memory_models  # noqa: E402,F401  (register memory/session offload tables on Base.metadata)
from acms import economics_models  # noqa: E402,F401  (register engineering-economics tables on Base.metadata)
from acms import jira_models  # noqa: E402,F401  (register jira reconciliation tables on Base.metadata)


@pytest.fixture(autouse=True)
def _schema_for_unit_tests():
    """Tests manage their own schema; the application itself must not create tables
    (see tests/test_schema_ownership.py). Alembic owns production schema."""

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield


@pytest.fixture()
def clean_db():
    """Wipe all rows between tests that need isolation (the unit DB is a single
    shared SQLite file created once per pytest run)."""
    from sqlalchemy import text

    async def _wipe() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys = OFF"))
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'DELETE FROM "{table.name}"'))
            await conn.execute(text("PRAGMA foreign_keys = ON"))

    asyncio.run(_wipe())
    yield
