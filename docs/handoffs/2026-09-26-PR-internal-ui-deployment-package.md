# Handoff — PR: First Live Internal UI + Deployment Package (ACMS-REQ-023, ACMS-REQ-046 partial)

**Date:** 2026-09-26
**Branch:** `feat/ACMS-046-live-internal-ui-deployment`
**Plan:** `ACMS_FIRST_LIVE_VM_AND_WEB_UI_DEPLOYMENT_PLAN_2026-09-25.md` — PR A only
**Status:** PR open, awaiting human review. **VM creation/deployment NOT started** (plan §19 human gate).

## 1. Assigned goal and requirement IDs

Build the internal web UI + deployment package as a single focused PR:

- `ACMS-REQ-023` — human roles (Administrator / Worker / Observer) via LLDAP group mapping.
- `ACMS-REQ-046` — **partial**: dashboard over real registry data only; missing fleet fields are explicitly labeled, not fabricated (plan §2).
- `ACMS-REQ-050` / `ACMS-REQ-051` — honored structurally: UI is an aggregate view over ACMS state; no per-agent browser connections; no per-action dependency added.
- ADR-0008 (Proposed) — server-rendered Jinja2 UI as the fastest path, intentionally replaceable by React/Next.js.

## 2. What was attempted / what changed

**Application (`acms/ui/`):**

- `roles.py` — LLDAP group DN → role mapping (case-insensitive, highest-privilege wins, unmapped → deny).
- `session_auth.py` — HMAC-SHA256 signed session cookies; `HttpOnly` + `SameSite=Lax` always, `Secure` under HTTPS; fail-closed login when `ACMS_SESSION_SECRET` is missing.
- `ldap_auth.py` — service bind → user search (`memberOf`) → user rebind verification → role mapping; sync `ldap3` calls in a worker thread (`anyio.to_thread`); all LDAP failures classify as 503 (fail closed, generic errors, no user enumeration).
- `routes.py` — `/ui/login`, `/ui/`, `/ui/agents`, `/ui/system`, `/ui/logout` + static; pages render real backend data only (version, health, counts by trust class, registry rows, environment, Alembic revision, uptime).
- Templates + dark-theme CSS (mobile-usable; ACMS-REQ-046 criterion).

**Wiring:** UI mounted in `main.py` via `install_ui(app)`; settings extended (session/LDAP vars, fail-closed); deps added (jinja2, python-multipart, ldap3, anyio); version → 0.2.0.

**Deployment (`deploy/`):** `Dockerfile` (py3.12, non-root, healthcheck), `compose.yaml` (acms-app + postgres + reverse-proxy; PG unpublished; app port compose-internal only), `reverse-proxy/nginx.conf` (HTTPS, allowlist 10.0.10/20, SSE-ready, forwarded headers), `.env.production.example` (placeholders only), `deploy.sh` / `update.sh` (refuses non-`origin/main` targets, records previous commit) / `smoke-test.sh`, runbook `README.md` (status/logs/restart/DB health/commit/revision/update/rollback/backup).

**Docs:** ADR-0008 (Proposed), README UI section, FUTURE_WORK FW-013/FW-014 (provenance Human-Directed), this handoff.

## 3. Current state

- Branch builds and all validation passes (see §4). No VM exists yet; MIAM-00135 untouched.
- UI login is **disabled by default** (no session secret → 503) — fail closed.

## 4. Validation

| Check | Command / Method | Result |
|---|---|---|
| Unit + UI tests | `pytest -q` (venv, PATH incl. venv bin) | **29 passed** (8 pre-existing + 21 new) |
| App import (no schema side effects) | `test_schema_ownership` in suite | Pass |
| UI auth flows | fake-ldap3 bind/search/rebind matrix, role matrix, cookie flags, token isolation | Pass |
| Bash syntax | `bash -n deploy/*.sh` | Pass |
| Compose/TLS config | `docker compose config` | Not run — Docker unavailable on this workstation (documented; validate on VM at deploy time) |
| Secret scan | `grep -r` patterns for token/password/secret/DSN in tracked files | Pass (placeholders only) |

Note: PostgreSQL integration tests require `alembic` on PATH — run from an activated venv or with the venv `bin` prepended to `PATH` (pre-existing environmental quirk, unrelated to this PR).

## 5. Blockers / assumptions / unresolved questions

1. **TLS certificate path** (plan §7): the PR does not create certificates. Deploy step needs the existing MARION internal CA cert/key at `/opt/acms/tls/`. If no trusted internal CA cert can be issued, plan §7 mandates stop-for-human-direction before exposing the login page.
2. **LLDAP group DNs** are placeholders in `.env.production.example` — operator sets real DNs; ACMS never creates/modifies LLDAP groups (plan §6).
3. **VMID/IP/DNS selection** happens at deployment time per plan §3/§11 preflight (explicit human deployment authorization still required).
4. Docker unavailable on this workstation — compose config validated by review only; `deploy.sh` includes per-step verification.

## 6. Reviewer focus

- `acms/ui/session_auth.py` + `ldap_auth.py` — auth security (signature verify, timing-safe compare, fail-closed paths, no user enumeration).
- `deploy/reverse-proxy/nginx.conf` — allowlist scope, no PG exposure, TLS settings.
- `deploy/compose.yaml` — no secrets in the file; all via `/opt/acms/.env`.
- Requirement honesty: ACMS-REQ-046 claimed **partial** only.

## 7. Recommended next action

Human review of the PR. After merge + explicit deployment approval, execute plan §11–§16 (VM on MIAM-00135, preflight, DNS, secrets, TLS, deploy, smoke + reboot tests, PBS backup check) and produce the deployment handoff per plan §21.
