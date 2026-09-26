# Handoff — PR #1 Async Stack Revision (ADR-0007 Accepted)

**Date:** 2026-09-26
**Repository:** `startupteams/acms-project-framework`
**Branch:** `feat/ACMS-001-control-plane-scaffold` (existing PR #1, still **unmerged**)
**Plan:** `ACMS_PR1_ASYNC_STACK_REVISION_PLAN_2026-09-25.md`
**Commits:** `9735cf6` (async persistence + Alembic + compose), `d01052f` (tests), `e9ed12c` (ADR/docs)

## 1. Requirement IDs addressed

- ACMS-REQ-001 (persistent agent registry — behavior preserved, now async)
- ACMS-REQ-003 (bridge-facing registry contract — public API semantics unchanged)
- ACMS-REQ-004 (capability metadata — unchanged)
- ACMS-REQ-031 (foundation only; A2A transport not yet implemented)
- ACMS-REQ-038 (async PostgreSQL deployment path now exercisable and validated)
- ACMS-REQ-039 (temporary bearer authentication, now explicitly Accepted in ADR-0007)

## 2. Files changed

New: `alembic.ini`, `compose.yaml`, `migrations/env.py`, `migrations/script.py.mako`,
`migrations/versions/0001_initial_agent_registry.py`, `scripts/validate_postgres_stack.py`,
`tests/test_async_registry.py`, `tests/test_postgres_integration.py`,
`tests/test_schema_ownership.py`.

Modified: `acms/db.py`, `acms/main.py`, `acms/registry.py`, `pyproject.toml`,
`tests/conftest.py`, `tests/test_app.py`, `.env.example`, `README.md`,
`docs/adr/0007-mvp-implementation-stack.md`,
`docs/handoffs/2026-09-25-PR001-control-plane-scaffold.md`.

## 3. ADR-0007 changes

- Status `Proposed` → **`Accepted`** (explicit human product-owner approval, 2026-09-25 session).
- Records: Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2.x **async API**
  (`create_async_engine`/`async_sessionmaker`/`AsyncSession`), `asyncpg`,
  PostgreSQL as the normal dev/integration/deployment DB, SQLite only for small
  isolated tests (`aiosqlite`), Alembic owns migrations (no startup `create_all`),
  pytest, A2A semantics + official A2A Python SDK preference when A2A code lands,
  HTTP+JSON/SSE MVP transport, temporary bearer token behind a replaceable auth
  boundary, monorepo note (frontend framework NOT selected).
- New "Language and rewrite principle" section: Python is the default, **not** an
  irreversible forever-language constraint; contracts/migrations/tests must be
  durable enough for later measured Go/Rust rewrites of hot components.
- Removed the obsolete consequence that synchronous SQLAlchemy was acceptable.

## 4. Sync → async persistence changes

- `acms/db.py`: only place engine/session creation exists —
  `create_async_engine` + `async_sessionmaker[AsyncSession]`; plain
  `postgresql://` URLs are auto-normalized to `postgresql+asyncpg://` (centralized
  in `async_database_url`).
- `acms/main.py`: `get_session` dependency yields `AsyncSession`; all DB handlers
  are `async def`; **`Base.metadata.create_all` removed from startup** (guarded by
  `tests/test_schema_ownership.py`, which imports the app in a fresh subprocess
  against a throwaway SQLite file and asserts no DB file is created).
- `acms/registry.py`: all persistence `async def` with awaited
  flush/commit/refresh; public behavior preserved — new registration creates a
  durable ACMS UUID identity; re-registering the same `external_registration_id`
  keeps the same `agent_id` (201 → 200); bridge/capability fields update; listing
  ordered by `created_at`; errors propagate.
- `acms/models.py` unchanged (already async-compatible declarative models).

## 5. Migration structure

- `alembic.ini` + `migrations/env.py` (async-capable env: converts sync URLs to the
  `+asyncpg` dialect, reads `ACMS_DATABASE_URL` via the app settings so there is one
  config source; no duplicate hardcoded credentials).
- `migrations/versions/0001_initial_agent_registry.py` — creates `agents`
  (uuid PK, unique `external_registration_id`, JSON capabilities, timestamps) +
  `ix_agents_external_registration_id`.
- Command: `alembic upgrade head` (documented in README).

## 6. PostgreSQL development/test instructions

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
export ACMS_POSTGRES_PASSWORD='<local dev password>'   # compose dev-only value
docker compose up -d postgres
export ACMS_DATABASE_URL=postgresql://acms:acms@localhost:5432/acms   # auto-asyncpg
export ACMS_ADMIN_TOKEN='<random local token>'
alembic upgrade head
uvicorn acms.main:app --reload
```

No-Docker fallback (used for validation here): dev extra `pgserver` runs an
embedded PostgreSQL 16 — the integration test module and the validation script use
it automatically when `ACMS_TEST_POSTGRES_URL`/`ACMS_DATABASE_URL` point nowhere.

## 7. Validation commands and exact results (all executed 2026-09-26)

| Check | Command | Result |
|---|---|---|
| Fast tests | `pytest -q` (Python 3.12.3 venv) | **8 passed**, 1 warning (starlette httpx deprecation, benign) |
| Async registry unit | `pytest -v` | `test_register_update_and_list_via_async_session PASSED` |
| Schema ownership | `pytest -v` | `test_importing_app_does_not_create_schema PASSED` |
| PG integration | `pytest -v` | `test_readiness_endpoints_on_live_app PASSED`, `test_register_and_list_through_asyncpg PASSED` (embedded PG 16, uvicorn subprocess) |
| Full PG stack | `python scripts/validate_postgres_stack.py` | alembic rc=0 on clean DB; `/health` 200 `{'status':'ok'}`; `/version` 200; register 201; re-register 200 same `agent_id`; list 200 count=1; unauthenticated list 401; **after uvicorn exit: agents rows = 1**, indexes `['agents_pkey','ix_agents_external_registration_id']` — `ALL POSTGRES STACK CHECKS PASSED` |
| Compile | `python -m compileall -q acms scripts migrations` | OK |
| Secret scan | grep of sk-/AKIA/private-key/ghp patterns over tracked files | clean (only documented placeholder DSNs `acms:acms` in `.env.example` and the `migrations/env.py` dev fallback) |

No lint/type-check tooling is configured in the repo, so none was run.

## 8. Known limitations / risks

- Bearer token is temporary MVP auth (ADR-0007); replace before external reachability.
- `alembic upgrade head` uses the same `ACMS_DATABASE_URL`; a plain `sqlite:///`
  URL is auto-converted to `sqlite+aiosqlite:///` for migrations.
- Integration tests skip cleanly when neither `ACMS_TEST_POSTGRES_URL` nor
  `pgserver` is available — CI should provide compose Postgres or accept the skip.
- Settings default `database_url` is still `sqlite:///./acms.db` for zero-config
  local experiments; `.env.example` and README direct normal development to
  PostgreSQL as the plan requires.
- Starlette deprecation warning (httpx vs httpx2 in TestClient) — cosmetic.

## 9. PR state

PR #1 (`feat/ACMS-001-control-plane-scaffold` → `main`) remains **OPEN and UNMERGED**.
Three follow-up commits pushed; PR body updated with this revision's facts. No
replacement PR opened. No scope beyond the approved correction was added.

## 10. Recommended human-review focus

1. `docs/adr/0007-mvp-implementation-stack.md` wording (now Accepted).
2. `migrations/env.py` URL-rewrite behavior (accepts plain `postgresql://` and
   rewrites to `+asyncpg`; single source of truth is `acms/settings.py`).
3. `acms/db.py` engine lifecycle (module-level engine; `dispose()` in tests).
4. Whether the settings default SQLite URL for zero-config is acceptable given
   `.env.example`/README now make PostgreSQL the documented norm.
5. Confirm merge readiness — merge is a human action; agents do not self-merge.
