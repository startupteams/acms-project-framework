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


@pytest.fixture(autouse=True)
def _schema_for_unit_tests():
    """Tests manage their own schema; the application itself must not create tables
    (see tests/test_schema_ownership.py). Alembic owns production schema."""

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield
