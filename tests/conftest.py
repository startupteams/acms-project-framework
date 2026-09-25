import os
from pathlib import Path

TEST_DB = Path("/tmp/acms-test.db")
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["ACMS_ADMIN_TOKEN"] = "test-token"
os.environ["ACMS_DATABASE_URL"] = f"sqlite:///{TEST_DB}"
