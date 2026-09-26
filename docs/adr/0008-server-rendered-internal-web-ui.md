# ADR-0008: Server-rendered internal web UI (Jinja2) as the first live control surface

**Status:** Proposed
**Date:** 2026-09-26

## Context

ACMS needs a usable internal web UI deployed on MARION-IA-USA quickly (deployment plan §1, §5). The approved dashboard requirement (ACMS-REQ-046) will ultimately need a rich frontend, but a full React/Next.js build pipeline would add weeks before any live control surface exists. The first deployment is intentionally a single-node internal MVP; Kubernetes, event buses, HA PostgreSQL, and multiple replicas are explicitly out of scope for this phase.

Human authentication must use the existing LLDAP service (plan §6). The bearer token is machine authentication only and must never reach browser JavaScript. The internal reverse-proxy/TLS pattern must be reused; plaintext HTTP is not acceptable for an LDAP-password login page.

## Decision

For the first live system, implement a thin **server-rendered web UI inside the existing FastAPI application**:

- Jinja2 templates + normal HTML/CSS; minimal browser JavaScript only where useful.
- No Node build pipeline for this first UI.
- Pages: `/ui/login`, `/ui/`, `/ui/agents`, `/ui/system`, `/ui/logout` (plus static assets).
- Human login via LLDAP (`ldap3`, run in a worker thread), mapped to Administrator / Worker / Observer roles through configurable group DNs; unmapped authenticated users are denied; a user with no mapped role is never granted access.
- Signed server-side session cookies (`HMAC-SHA256`), `HttpOnly` + `SameSite=Lax`, `Secure` under HTTPS; fail-closed when `ACMS_SESSION_SECRET` is unset.
- Deployment as one VM with Docker Compose: `acms-app` + `postgres` + `reverse-proxy` (nginx HTTPS, internal source networks only).

This UI is intentionally replaceable: a future React/Next.js frontend would consume the same ACMS API surface, so the templates are a temporary control surface, not an architecture commitment.

## Consequences

- **Easier:** fastest path to a real internal control surface; no frontend toolchain to build, host, or update; one deployable unit; session handling stays server-side (no token in JS).
- **Harder:** interactive/real-time dashboard features (live agent streams, Kanban drag-and-drop) will eventually demand a proper frontend; the Jinja2 layer will be discarded when that happens.
- **Risks/limits:** ACMS-REQ-046 is only partially satisfied — the UI shows registry data that exists today and explicitly labels missing fields (primary assignment, status, last contact, cost) as not yet implemented rather than fabricating them.
- **Reversibility:** templates/routes are isolated under `acms/ui/`; replacing them does not affect the registry, persistence, or API layers.

## References

- Requirement / issue / PR: ACMS-REQ-023, ACMS-REQ-046 (partial), ACMS-REQ-025 (via audit-ready actions deferred), ACMS-REQ-050/051 (architecture unaffected)
- Related ADR/TDR: ADR-0007 (stack), ADR-0003 (A2A bridge direction)
- Source: `ACMS_FIRST_LIVE_VM_AND_WEB_UI_DEPLOYMENT_PLAN_2026-09-25.md` §5–§9
