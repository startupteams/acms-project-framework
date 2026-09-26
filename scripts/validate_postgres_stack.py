"""Standalone PostgreSQL stack validation for ACMS PR #1 revision (ADR-0007).

Proves, end to end, with real output captured:
1. Alembic upgrades a CLEAN PostgreSQL database from zero to current schema.
2. The app starts against PostgreSQL and /health + /version respond.
3. Authenticated registration works over the async asyncpg stack.
4. Re-registration preserves the ACMS agent_id (201 -> 200, same UUID).
5. Agent listing works; unauthenticated access is rejected.
6. Rows persist in PostgreSQL after the application process exits.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pgserver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from acms.db import async_database_url

PGDATA = "/tmp/acms-manual-pgdata-2"
TOKEN = "manual-Va...oken"


def main() -> int:
    srv = pgserver.get_server(PGDATA, cleanup_mode="delete")
    url = srv.get_uri()
    print(f"1. embedded PostgreSQL 16 URI scheme: {url.split('://')[0]}://... (clean data dir)")

    env = {**os.environ, "ACMS_DATABASE_URL": url, "ACMS_ADMIN_TOKEN": TOKEN}

    r = subprocess.run(["alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
    print(f"2. alembic upgrade head rc={r.returncode}")
    for line in r.stdout.strip().splitlines():
        print("   ", line)
    assert r.returncode == 0, r.stderr

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "acms.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        health = None
        while time.time() < deadline:
            if proc.poll() is not None:
                print("UVICORN DIED:\n", proc.stdout.read() if proc.stdout else "")
                return 1
            try:
                resp = httpx.get(f"{base}/health", timeout=2)
                if resp.status_code == 200:
                    health = resp
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        assert health, "app did not become healthy"
        print(f"3. GET /health -> {health.status_code} {health.json()}")
        ver = httpx.get(f"{base}/version")
        print(f"   GET /version -> {ver.status_code} {ver.json()}")

        auth = {"Authorization": f"Bearer {TOKEN}"}
        ext = f"manual-validate-{uuid.uuid4()}"
        payload = {
            "external_registration_id": ext,
            "display_name": "Manual Validation Agent",
            "trust_class": "internal",
            "harness": "hermes",
            "bridge_version": "0.1",
            "protocol_version": "a2a",
            "capabilities": {"streaming": True},
        }
        r1 = httpx.post(f"{base}/api/v1/agents/register", headers=auth, json=payload)
        print(f"4. register -> {r1.status_code} agent_id={r1.json()['agent_id']}")
        assert r1.status_code == 201

        payload["bridge_version"] = "0.2"
        r2 = httpx.post(f"{base}/api/v1/agents/register", headers=auth, json=payload)
        same = r2.json()["agent_id"] == r1.json()["agent_id"]
        print(f"5. re-register -> {r2.status_code} identity preserved: {same} bridge_version now {r2.json()['bridge_version']}")
        assert r2.status_code == 200 and same

        r3 = httpx.get(f"{base}/api/v1/agents", headers=auth)
        print(f"6. list -> {r3.status_code} count={len(r3.json())}")
        assert r3.status_code == 200

        r4 = httpx.get(f"{base}/api/v1/agents")
        print(f"7. unauthenticated list -> {r4.status_code} (expected 401/403)")
        assert r4.status_code in (401, 403)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    async def check() -> tuple[int, list[str]]:
        engine = create_async_engine(async_database_url(url))
        async with engine.connect() as conn:
            n = (await conn.execute(text("select count(*) from agents"))).scalar()
            idx = (
                await conn.execute(text("select indexname from pg_indexes where tablename='agents'"))
            ).scalars().all()
        await engine.dispose()
        return n, idx

    n, idx = asyncio.run(check())
    print(f"8. after uvicorn exit: agents rows in PostgreSQL = {n}, indexes = {idx}")
    assert n >= 1
    print("ALL POSTGRES STACK CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
