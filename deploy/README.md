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
| `/opt/acms/repo` | Git checkout of approved `main` (production only, never dev work) |
| `/opt/acms/releases/` | Release state: ledger, metadata, pre-deploy DB backups |
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

## Safe release transaction (feature-delivery plan §7)

```bash
deploy/release.sh              # release to origin/main HEAD (approved commit)
deploy/release.sh <sha>        # release to a specific approved main commit
```

`release.sh` runs the full transaction: preflight → maintenance window →
verified pre-deploy PostgreSQL backup → SHA-tagged image build → alembic
upgrade → two-stage validation (direct + proxy) → accept, or **automatic
rollback** to the exact previous known-good release if any validation fails.
Deployment remains human-gated (plan §3); rollback on failed validation is
pre-authorized as a safety action.

Release state lives outside Git at `/opt/acms/releases/`:

```text
/opt/acms/releases/
├── current            # release id of the accepted release
├── previous           # release id of the previous known-good release
├── history.jsonl      # append-only release/rollback ledger
├── releases/<id>.json # full release metadata (§4 fields, no secrets)
├── transaction.json   # open release transaction (guards Level-2 rollback)
└── backups/<id>.dump  # pre-deploy pg_dump -Fc, SHA-256 recorded
```

## Rollback (feature-delivery plan §8/§9)

```bash
deploy/rollback.sh             # to the immediately previous known-good release
deploy/rollback.sh <release-id>
```

The script chooses the lowest reliable level:

| Level | When | Action |
|---|---|---|
| 0 | transient failure, same code/schema | restart container, re-validate |
| 1 | broken app, schema compatible | previous image/SHA, **no downgrade** |
| 2 | migration ran / state suspicious | app rollback **+ verified DB restore** (guarded) |
| 3 | VM/PBS disaster recovery | restore whole VM from PBS (manual, see below) |

Guards (plan §29): Level-2 DB restore only runs inside an open release
transaction (i.e. validation failed mid-release), with a checksum-verified
backup; standalone rollback is application-only and never touches the
database. The script refuses unknown releases, never blind-downgrades Alembic,
and never hides a failed rollback — if a guard fails it stops and requests
human direction.

If a released build shows a severe regression, a human can simply instruct:
`rollback ACMS`.

## Disaster recovery layer (plan §30)

- Git — source history and previous application versions.
- Pre-deploy PostgreSQL dumps — fast deterministic app+DB rollback.
- PBS (`marion-pbs-daily-all`, 03:05) — whole-VM disaster recovery. Restore VM
  from PBS → `deploy/smoke-test.sh` → verify registry rows via `/ui/agents`
  (or `SELECT count(*) FROM agents`).

No single layer substitutes the others.

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
