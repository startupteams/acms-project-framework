"""ADR-0007 PostgreSQL integration path: real PostgreSQL 16 + Alembic + running app.

Boots the actual app as a uvicorn subprocess against a freshly-migrated clean
PostgreSQL database, proving: readiness endpoints work with no schema present,
Alembic builds schema from zero, and registration/listing run through the async
asyncpg stack exactly as in deployment.

Server selection (first available):
1. ACMS_TEST_POSTGRES_URL environment variable (e.g. from `docker compose up -d postgres`);
2. pgserver — embedded PostgreSQL 16, no Docker/root required (dev-machine fallback).

Skips cleanly when neither is available (e.g. restricted CI).
"""

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
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
        pytest.skip("No PostgreSQL available (set ACMS_TEST_POSTGRES_URL or install pgserver)")
    return url


def _env(url: str) -> dict[str, str]:
    return {**os.environ, "ACMS_DATABASE_URL": url, "ACMS_ADMIN_TOKEN": "integration-token"}


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(
        ["alembic", *args], cwd=REPO, env=_env(url), capture_output=True, text=True
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_app(pg_url: str):
    """uvicorn subprocess running against the clean, freshly-migrated PostgreSQL."""
    _alembic(pg_url, "upgrade", "head")
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "acms.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO,
        env=_env(pg_url),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"uvicorn exited early:\n{proc.stdout.read() if proc.stdout else ''}")
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail("uvicorn did not become healthy within 30s")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


TOKEN = "integration" + "-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_readiness_endpoints_on_live_app(live_app: str):
    assert httpx.get(f"{live_app}/health").json() == {"status": "ok"}
    assert "version" in httpx.get(f"{live_app}/version").json()


def test_register_and_list_through_asyncpg(live_app: str):
    external_id = f"pg-integration-{uuid.uuid4()}"
    payload = {
        "external_registration_id": external_id,
        "display_name": "PG Integration Agent",
        "trust_class": "internal",
        "harness": "hermes",
        "bridge_version": "0.1",
        "protocol_version": "a2a",
        "capabilities": {"streaming": True},
    }
    created = httpx.post(f"{live_app}/api/v1/agents/register", headers=AUTH, json=payload)
    assert created.status_code == 201, created.text
    agent_id = created.json()["agent_id"]

    payload["bridge_version"] = "0.2"
    again = httpx.post(f"{live_app}/api/v1/agents/register", headers=AUTH, json=payload)
    assert again.status_code == 200
    assert again.json()["agent_id"] == agent_id  # stable ACMS identity through asyncpg

    listed = httpx.get(f"{live_app}/api/v1/agents", headers=AUTH)
    assert listed.status_code == 200
    assert any(a["agent_id"] == agent_id for a in listed.json())
