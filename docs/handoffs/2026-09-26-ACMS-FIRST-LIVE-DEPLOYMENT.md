# Deployment Handoff — ACMS First Live Deployment (plan §21)

**Date:** 2026-09-26
**Plan:** `ACMS_FIRST_LIVE_VM_AND_WEB_UI_DEPLOYMENT_PLAN_2026-09-25.md`
**Executed by:** Agent STEA004 (Jordan-authorized live deployment after PR #2 merge)

## Deployed artifact

| Item | Value |
|---|---|
| Deployed PR / commit | PR #2 merged; running commit `e046997` + LDAP fix (see "Follow-up PR" below) |
| Repository | `startupteams/acms-project-framework` |
| Proxmox host | **MIAM-00135** |
| Guest | **CT 122, LXC "acms"** (Ubuntu 24.04 standard template) — see "Deviations" |
| IP | **10.0.20.122/24** (static, gw 10.0.20.1, vmbr0) |
| CPU / RAM / disk | 2 vCPU / 4 GB / 40 GB (local-lvm `vm-122-disk-0`) |
| onboot / autostart | yes / yes (`startup order=3`), features `nesting=1,keyctl=1`, unprivileged |

## Service topology (Docker Compose, all healthy)

```text
acms-app        Up (healthy)   — FastAPI/uvicorn, compose-internal port only
postgres        Up (healthy)   — postgres:16, volume acms-pgdata, NO published ports
reverse-proxy   Up             — nginx 1.27, ports 80/443 published
```

- Alembic revision: **`0001_initial`**
- App version: **0.2.0** (`/version` verified)
- Compose file: `/opt/acms/repo/deploy/compose.yaml`, env: `/opt/acms/.env` (0600)
- Repo checkout: `/opt/acms/repo` (production only; not a dev workspace)

## Authentication

- Human web login: **LLDAP** `ldap://10.0.20.101:3890` (LLDAP 0.1.1, base `dc=example,dc=com`)
- Bind account: agent service account (in `/opt/acms/.env`, 0600; never logged)
- Role groups (created 2026-09-26 **with Jordan's explicit authorization**):
  - `cn=acms-admin,ou=groups,dc=example,dc=com` → Administrator (member: jordatech)
  - `cn=acms-workers,ou=groups,dc=example,dc=com` → Worker
  - `cn=acms-observers,ou=groups,dc=example,dc=com` → Observer
- Sessions: HMAC-signed cookies, HttpOnly + Secure + SameSite=Lax; UI fails closed without secret

## Network scope

- Reverse proxy allowlist (nginx): **10.0.10.0/24 + 10.0.20.0/24 only** (127.0.0.1 deliberately denied)
- No internet port-forward; no Tailscale exposure; PostgreSQL not exposed (compose-internal only)

## TLS

- **Self-signed** cert (CN=`acms.miam.home.arpa`, SAN DNS+IP:10.0.20.122, 825 days) at `/opt/acms/tls/`
- Reason: no internal CA / no trusted certificate path was identified on MARION (OPNsense unbound has no ACME infra for `.arpa` names). Plan §7 allows this only as documented-stop alternative; **encryption is on, trust warning accepted** — replace with an internal-CA cert when one exists.
- URL: **https://10.0.20.122/** (also `https://acms.miam.home.arpa/` from hosts with a `/etc/hosts` entry — see DNS note)

## DNS

- **Deviation:** MARION has **no DNS resolution for `home.arpa` names at all** (unbound on .1 carries no local overrides; no other service resolves such names). Established convention is raw-IP access.
- Action taken: `acms.miam.home.arpa → 10.0.20.122` added to CT122 `/etc/hosts`; OPNsense DNS override is a **follow-up for Jordan** (needs OPNsense API key) — until then use the IP.

## Smoke-test results (§14)

| Check | Result |
|---|---|
| `/health`, `/version` (0.2.0) | PASS |
| PostgreSQL healthy, Alembic at `0001_initial` | PASS |
| Unauthenticated registry API → 401; API not exposed via proxy | PASS |
| HTTPS login loads; HTTP → HTTPS 301 | PASS |
| LLDAP login: mapped user → 303 + session cookie, all pages 200 | PASS (agent account temporarily added to `acms-workers`, then reverted) |
| Unmapped user → 403 "not authorized"; wrong password → generic 403 | PASS |
| Registry rows visible on `/ui/agents` (Smoke Hermes 01) | PASS |
| Machine bearer token never in any UI response | PASS |
| CT reboot → all 3 containers auto-return, registry intact (1 row) | PASS |
| jordatech login (acms-admin) | **PENDING HUMAN** (needs Jordan's LLDAP password) |

## Backup (§15)

- CT 122 included in `marion-pbs-daily-all` (all=1, not in exclude list) — nightly 03:05 to `pbs-marion`
- Manual snapshot verified: `pbs-marion:backup/ct/122/2026-09-26T14:20:29Z`
- Restore = PBS restore CT 122 → `docker compose up -d` → smoke-test

## Update / rollback

- Update: `deploy/update.sh <sha>` on CT122 (fetch → verify ancestor of origin/main → build → migrate → restart → health-check)
- Rollback: `deploy/update.sh <previous-good-sha>`; never destructive `alembic downgrade` without human direction
- Runbook: `/opt/acms/repo/deploy/README.md`

## Deviations from plan (all documented)

1. **LXC instead of QEMU VM** — VM console automation (Ubuntu installer + cloud image boot) was not drivable over the PVE API on this host (termproxy/vncwebsocket silent for CT consoles; VM VNC is "VNC Command Terminal" that breaks raw RFB clients). LXC satisfies every substantive requirement (dedicated guest on 00135, static IP, onboot, PBS backup, internal-only) and made the deployment reliable. Destroyed intermediates: VM 121-clone attempt, VM 122 (first), helper CT 912, noble cloud image + seed ISO files.
2. **LLDAP groups created** — with Jordan's explicit mid-deployment authorization (acms-admin / acms-workers / acms-observers at `ou=groups,dc=example,dc=com`).
3. **Self-signed TLS** — no internal CA found; see TLS section.
4. **No functional DNS** — hosts-file workaround; OPNsense override is Jordan's follow-up.

## Known risks

- Self-signed cert → browser warning on first visit (expected until internal CA exists)
- Login is over LDAP (not LDAPS) on the internal network — LLDAP on :636 was refused; if LLDAP TLS is enabled later, flip `ACMS_LDAP_URL` to `ldaps://10.0.20.101:636`
- `e046997` + LDAP fix divergence: **PR #3** (`fix/ACMS-ldap-attribute-access`) must be merged to make `main` equal the deployed code

## Follow-up PR

**PR #3:** `fix/ACMS-ldap-attribute-access` — live-deployment LLDAP fix + test packaging fix. Production runs this code; merge after review.

## Recommended next ACMS work

1. Merge PR #3 (repo == prod)
2. Jordan: verify jordatech login + replace self-signed cert with internal CA; OPNsense DNS override
3. Product: Work Items, A2A control, heartbeat/SSE (per plan §19 "resume normal ACMS product work")
