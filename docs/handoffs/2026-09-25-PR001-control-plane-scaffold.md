# PR-001 Handoff - ACMS Control Plane Scaffold

## Requirements addressed

- ACMS-REQ-001 (partial/initial persistent identity implementation)
- ACMS-REQ-003 (initial bridge-facing registry contract)
- ACMS-REQ-004 (capability metadata representation)
- ACMS-REQ-031 (foundation only; transport not yet implemented)
- ACMS-REQ-038 (async PostgreSQL deployment path now exercisable; see validation)
- ACMS-REQ-039 (minimal bearer authentication; temporary, documented in ADR-0007)

## Changes

- Added FastAPI backend scaffold (async request handlers throughout).
- Added SQLAlchemy 2.x **async** persistence layer (`AsyncSession`, `asyncpg` driver);
  all runtime persistence operations are asynchronous per ADR-0007.
- Added PostgreSQL as the normal development/deployment database:
  `compose.yaml` PostgreSQL 16 service and `pgserver` embedded fallback for Docker-free machines.
- Added Alembic migration management (`alembic upgrade head`); the application does not
  create schema on startup (protected by `tests/test_schema_ownership.py`).
- Added persistent agent registry keyed by external registration ID with stable ACMS UUID.
- Added explicit Agent Bridge/capability metadata model.
- Added authenticated register/list endpoints plus health/version readiness endpoints.
- Added unit/API tests (isolated SQLite via `aiosqlite`) and a PostgreSQL integration
  path (Alembic upgrade on a clean database + uvicorn subprocess against real
  PostgreSQL; skips cleanly when no server is available).
- ADR-0007 updated to **Accepted**: async persistence mandated, Python-first language
  principle documented.

## Validation

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q                        # unit/API tests (aiosqlite) + PG integration path
alembic upgrade head             # against ACMS_DATABASE_URL (PostgreSQL)
uvicorn acms.main:app --reload
```

PostgreSQL without Docker: `pip install pgserver` (included in dev extras); the
integration test module starts an embedded PostgreSQL 16 automatically. With
Docker: `export ACMS_POSTGRES_PASSWORD='<set a local dev password>' && docker compose up -d postgres` and set
`ACMS_TEST_POSTGRES_URL`/`ACMS_DATABASE_URL` accordingly. No real credentials are
required in repository files or examples.

## Known limitations

- This is not yet a full A2A server/client implementation.
- Bearer token authentication is a temporary MVP mechanism (ADR-0007); a secure
  credential store is required before external reachability.
- No roles, audit event store, heartbeat, SSE, work-item model, or dashboard yet.
- The PostgreSQL integration path skips in environments without a server; CI should
  provide one (compose service or `ACMS_TEST_POSTGRES_URL`).

## Human decisions required

- Confirm ADR-0007 as Accepted (async stack revision) or request changes.
- Confirm the scaffold may proceed to full agent registration/capability discovery implementation.

## Recommended next action

Human review of this PR. Do not merge autonomously.
