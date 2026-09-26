"""ADR-0007: application startup/imports must not create schema — Alembic owns migrations.

Runs in a fresh subprocess with a throwaway SQLite URL so that schema creation on
import cannot be masked by earlier tests creating the shared test database.
"""

import os
import subprocess
import sys
from pathlib import Path

PROBE_DB = Path("/tmp/acms-no-schema-probe.db")


def test_importing_app_does_not_create_schema():
    if PROBE_DB.exists():
        PROBE_DB.unlink()
    env = {**os.environ, "ACMS_DATABASE_URL": f"sqlite:///{PROBE_DB}", "ACMS_ADMIN_TOKEN": "x"}
    result = subprocess.run(
        [sys.executable, "-c", "import acms.main"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not PROBE_DB.exists(), "importing acms.main created a database file — startup must not manage schema"
