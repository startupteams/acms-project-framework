# ADR-0007: Select the initial ACMS backend stack and MVP machine authentication

**Status:** Accepted  
**Date:** 2026-09-25

## Context

The first executable ACMS slice needs a simple stack that AI coding agents can maintain, can expose HTTP/SSE/A2A integrations, and can move from local development to PostgreSQL-backed deployment. The stack is a significant architecture decision, so it was drafted as Proposed in PR #1 and explicitly approved by the human product owner during the 2026-09-25 ACMS architecture co-work session. The approval requires asynchronous persistence as part of the MVP foundation.

## Decision

Use:

- Python 3.12+;
- FastAPI for the control-plane API;
- Pydantic for external/data contracts and validation;
- SQLAlchemy 2.x for persistence using its **asynchronous** API (`create_async_engine`, `async_sessionmaker`, `AsyncSession`);
- `asyncpg` as the PostgreSQL async driver;
- PostgreSQL as the normal development, integration-test, and deployment database;
- SQLite only for small isolated/unit tests where PostgreSQL-specific behavior is not under test; use the `aiosqlite` driver when async SQLAlchemy is exercised against SQLite;
- Alembic for versioned database schema migrations. Application startup and imports must not create schema (`Base.metadata.create_all` is not used in development/deployment);
- pytest for automated tests;
- A2A as the primary agent-communication semantic foundation; when ACMS begins implementing A2A, prefer the official A2A Python SDK rather than inventing an unrelated protocol implementation;
- HTTP+JSON with SSE where streaming is needed as the initial MVP transport direction;
- a configured bearer token as the minimal machine-authentication boundary for the first private-network scaffold, kept behind a replaceable authentication interface. Evaluate stronger short-lived credentials and/or mTLS before external/customer connectivity.

The repository is a monorepo. A future frontend may be React/Next.js/TypeScript or another suitable web stack inside the same repository; the frontend framework is **not** selected by this ADR.

## Language and rewrite principle

> Use Python for ACMS backend services, agent integration, tooling, evaluation, and automation unless measurements or a concrete architectural need justify another language.

Python is the default, not an irreversible forever-language constraint. It was selected primarily to maximize product iteration speed and AI-agent maintainability while the product behavior, workflow model, agent-management model, context model, and human-control experience are still being discovered.

ACMS protocol contracts, API schemas, database migrations, event semantics, and automated tests shall be documented and tested well enough that performance-sensitive components — for example a future external agent gateway, event-ingest service, or Agent Bridge — can later be reimplemented in Go or Rust without changing the ACMS domain model or A2A-facing contracts. Rust/Go are future optimization options, not current MVP requirements; rewrites must be driven by measured bottlenecks or a concrete operational need, and the architecture must not be prematurely optimized for extreme scale.

## Consequences

### Benefits

- Fast implementation and strong Python/AI/LLM/A2A ecosystem fit.
- Async persistence matches the HTTP+JSON/SSE serving model and keeps the event loop unblocked under concurrent agent traffic.
- Easy HTTP/SSE integration and simple testability (fast SQLite-backed unit tests; PostgreSQL container for integration).
- Versioned Alembic migrations give a deterministic PostgreSQL schema path from zero.
- Durable contracts preserve the option to rewrite individual services later.

### Costs/Risks

- A static bearer token is not sufficient as the long-term identity design; replacement is planned before external/customer connectivity.
- Async SQLAlchemy requires disciplined session/lifecycle management; the codebase centralizes engine/session creation to contain this.
- PostgreSQL is required for realistic integration testing; a minimal development container is provided.

## References

- ACMS-REQ-001
- ACMS-REQ-003
- ACMS-REQ-031
- ACMS-REQ-038
- ACMS-REQ-039
