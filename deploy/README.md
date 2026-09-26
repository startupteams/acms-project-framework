# ACMS Deployment & Operations Runbook (first live VM, plan §16)

Single-node Docker Compose deployment of the ACMS control plane with an
internal server-rendered web UI, LLDAP human login, and internal-only HTTPS.

```text
Authorized Browser ──HTTPS──> reverse-proxy ──> acms-app ──> postgres
                                 (nginx)        (FastAPI)    (volume: acms-pgdata)
```

## Production layout

| Path | Purpose |
|---|---|
| `/opt/acms` | Git checkout of approved `main` (production only, never dev work) |
| `/opt/acms/.env` | Secrets, `0600` (never committed; see `.env.production.example`) |
| `/opt/acms/tls/acms.crt` | TLS full chain (internal CA) |
| `/opt/acms/tls/acms.key` | TLS private key, `0600` |
| `deploy/` | Compose stack + this runbook (from the repo checkout) |

## First deployment

```bash
cd /opt/acms
git fetch origin main && git checkout <approved-main-sha>   # exact approved commit
cp deploy/.env.production.example /opt/acms/.env            # then fill real values
chmod 600 /opt/acms/.env
mkdir -p /opt/acms/tls && cp <your>.crt /opt/acms/tls/acms.crt
cp <your>.key /opt/acms/tls/acms.key && chmod 600 /opt/acms/tls/acms.key
deploy/deploy.sh
```

`deploy.sh` builds the image, starts PostgreSQL, waits for DB health, runs
`alembic upgrade head`, starts the app + reverse proxy, and runs smoke tests.

## Operator commands (wrappers; run from `/opt/acms`)

```bash
# Status
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env ps

# Logs
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env logs -f acms-app
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env logs --tail 100 reverse-proxy

# Restart ACMS app
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env restart acms-app

# Restart reverse proxy
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env restart reverse-proxy

# Database health
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env exec postgres pg_isready -U acms -d acms

# Current deployed commit
git -C /opt/acms rev-parse HEAD

# Current Alembic revision
docker compose -f deploy/compose.yaml --env-file /opt/acms/.env \
  exec postgres psql -U acms -d acms -tAc 'SELECT version_num FROM alembic_version'
```

## Deploy / update flow (plan §17)

```bash
deploy/update.sh              # update to origin/main HEAD
deploy/update.sh <sha>        # update to a specific approved commit
```

`update.sh` records the previous commit, fetches `origin/main`, refuses any
commit that is not an ancestor of `origin/main`, checks out the target,
rebuilds, migrates, restarts, health-checks, and prints both commits.
Continuous/automatic deployment is intentionally not used.

## Rollback (plan §18)

1. `git -C /opt/acms rev-parse HEAD` — confirm the currently deployed commit.
2. Identify the previous known-good commit (`git log --oneline -n 10`).
3. `deploy/update.sh <previous-sha>` — migrations on this system so far are
   additive; rolling the app back to an older commit is expected to be safe.
4. **Never** run destructive `alembic downgrade` without human direction. If a
   future migration is not backward-compatible, stop and involve a human.

## Smoke test (plan §14)

```bash
deploy/smoke-test.sh                       # against https://acms.miam.home.arpa
deploy/smoke-test.sh https://<vm-ip>       # against the VM IP (TLS SNI caveat)
```

Checks: `/health`, `/version`, unauthenticated API denial, HTTPS login page,
HTTP→HTTPS redirect, UI redirect for unauthenticated browsers, and that the
registry API is not exposed through the proxy. Full human checks (LLDAP login
per role, browser trust of the internal CA, reboot persistence) are listed in
the PR description and the deployment handoff.

## Backup (plan §15)

- Include the ACMS VM in the normal Proxmox/PBS backup policy
  (`marion-pbs-daily-all`, 03:05 snapshot).
- The PostgreSQL data lives in the named volume `acms-pgdata` on the VM's
  backed-up storage — verify its location on the VM during deployment and
  record it in the handoff.
- Restore = restore VM from PBS → `deploy/smoke-test.sh` → verify registry rows
  via `/ui/agents` (or `SELECT count(*) FROM agents`).

## Known limitations (first MVP)

- Server-rendered Jinja2 UI, intentionally replaceable by React/Next.js (ADR-0008).
- Assignment/status/last-contact/cost do not exist in the backend yet — the UI
  never fabricates them (ACMS-REQ-046 is only partially satisfied).
- No privileged UI actions; all pages are read views.
