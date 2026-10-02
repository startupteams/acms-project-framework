# MIAM MCP Gateway (Window 1)

Thin internal MCP gateway for Startup Teams managed agents. Approved plan:
2026-10-02 STEA-004 (Jira Workflow Recovery and Internal MCP Gateway), §9–§20.
Requirement: **ACMS-REQ-064**. ADRs: 0019 (boundary), 0020 (tokens),
0021 (risk classes), 0022 (context manifest).

## What it is

- One logical MCP endpoint (streamable HTTP at `/mcp`, stateless JSON mode)
  on **VM114:8202**, service `miam-mcp-gateway`.
- Agent-scoped + assignment-scoped bearer tokens (hash-only storage).
- W1 domain adapter: **acms.\*** (9 resources + 6 worker tools) calling ACMS
  REST — ACMS keeps domain authority; the gateway never opens the ACMS DB.
- Durable MCP activity log (SQLite + optional JSONL mirror); arguments hashed.
- Fail-closed policy: DESTRUCTIVE denied for everyone (W1); SENSITIVE_WRITE
  needs executive/infrastructure-admin; workers write only their own assignment.

Existing REST / A2A / SSE remain authoritative; the gateway is additive and
independently fail-able.

## Deploy (VM114)

```bash
# 1. rsync the repo (or a release artifact) to VM114:/opt/mcp-gateway/repo
rsync -a --exclude .venv* --exclude .git --exclude __pycache__ \
  ~/work/acms-project-framework/ vm114:/opt/mcp-gateway/repo/

# 2. env file (edit values, NEVER commit real ones)
ssh vm114 'mkdir -p /etc/miam-mcp-gateway && chmod 700 /etc/miam-mcp-gateway'
scp deploy/mcp-gateway/env.example vm114:/etc/miam-mcp-gateway/env
ssh vm114 'editor /etc/miam-mcp-gateway/env'   # set ACMS_SERVICE_TOKEN etc.

# 3. install (creates venv, unit, enables service, health checks)
ssh vm114 'bash /opt/mcp-gateway/repo/deploy/mcp-gateway/install-gateway.sh'
```

## Tokens (operator)

```bash
ssh vm114 'cd /opt/mcp-gateway/repo && .venv-mcp/bin/python -m mcp_gateway.cli \
  mint-agent acms-hermes-worker-uid-005 --acms-agent-id <uuid>'
# -> prints the RAW token ONCE (capture into the worker credential file, 0600)

python -m mcp_gateway.cli mint-assignment acms-hermes-worker-uid-005 \
  ACMS-WORK-000018-... --agent-token <raw-from-mint> --hours 24 \
  --project-slug acms-control-plane --jira STNA-88 \
  --repo startupteams/acms-project-framework

python -m mcp_gateway.cli list-tokens
python -m mcp_gateway.cli revoke <token_id> "why"
python -m mcp_gateway.cli activity --denied
```

## Capability surface (W1)

Resources (READ, all authenticated):

| URI | Meaning |
|---|---|
| `acms://work/{work_uid_or_id}` | Work Item by UID/key/UUID (scope-checked) |
| `acms://assignment/current` | caller's ACTIVE assignment + work |
| `acms://project/{project_ref}` | Project by id/slug (assignment binding enforced) |
| `acms://product/{product_id}` | Product by id |
| `acms://artifact/{artifact_ref}` | Artifact metadata + content |
| `acms://agent/self` | caller's own agent identity |
| `acms://policy/current` | effective policy snapshot + denials |
| `acms://inbox/current` | inbox rows (workers: own) |
| `acms://context/current` | assignment context manifest (plan §14) |

Tools (SAFE_WRITE, own-assignment scope, audited):

`acms.work.update_progress` · `acms.work.mark_blocked` · `acms.artifact.create`
· `acms.work.submit_handoff` · `acms.inbox.raise` · `acms.review.request`

## Tests

`tests/test_mcp_gateway.py` — 25 tests: token lifecycle (mint/expiry/revocation
+ binding), policy gates (destructive/sensitive/role/scope), URI matching,
protocol E2E over stateless HTTP (health public, 401s audited, tools/list with
dotted names + `_meta`, resources + templates listing, work read, manifest,
agent-self, out-of-scope denial, handoff/artifact/progress/blocked/review/
inbox tools, audit ok+denied without raw tokens, expired/revoked fail-closed,
multi-tenant isolation).

## Security invariants (plan §43)

- No generic root-shell capability exists; never add one.
- DESTRUCTIVE capabilities are not registered in W1; adding one later needs an
  ADR amendment.
- Tokens never appear in logs, audit rows, or handoffs (hash-only).
- Gateway failure never breaks ACMS REST/A2A/SSE (separate process/host).
