# ADR-0007: Select the initial ACMS backend stack and MVP machine authentication

**Status:** Proposed  
**Date:** 2026-09-25

## Context

The first executable ACMS slice needs a simple stack that AI coding agents can maintain, can expose HTTP/SSE/A2A integrations, and can later move from local development to PostgreSQL-backed deployment. The exact stack was not explicitly selected during product discovery and therefore requires human approval.

## Proposed Decision

Use:

- Python 3.12;
- FastAPI for the control-plane API;
- Pydantic for external/data contracts;
- SQLAlchemy 2.x for persistence with PostgreSQL as the deployment target and SQLite allowed for local tests/development;
- pytest for automated tests;
- HTTP/SSE for the first A2A/Agent Bridge integration.

For the first private-network scaffold, use a configured bearer token as a minimal machine-authentication boundary while keeping authentication behind a replaceable interface. Evaluate stronger short-lived credentials and/or mTLS before external/customer connectivity.

## Consequences

### Benefits

- Fast implementation and strong Python/A2A ecosystem fit.
- Easy HTTP/SSE integration.
- Simple testability.
- PostgreSQL path without requiring it for local unit tests.

### Costs/Risks

- A static bearer token is not sufficient as the long-term identity design.
- Synchronous SQLAlchemy is intentionally simple and may later be replaced/augmented if measured concurrency requires it.
- Human approval is required before this ADR becomes Accepted.

## References

- ACMS-REQ-001
- ACMS-REQ-003
- ACMS-REQ-031
- ACMS-REQ-038
- ACMS-REQ-039
