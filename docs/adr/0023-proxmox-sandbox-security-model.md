# ADR-0023 — Proxmox Sandbox Security Model (plan §26)

**Status:** Proposed
**Date:** 2026-10-02
**Owner:** STEA-004 (agent-drafted; human acceptance pending)
**Requirement:** ACMS-REQ-064 (internal MCP gateway); plan §26

## Context

Workers need compute they can shape — install packages, run Docker, compile,
test, run services — without receiving generic Proxmox root access. The plan
§26 names the middle ground: **sandboxes** (owned, tagged, resource-limited,
non-critical, TTL'd VMs) and a **narrow production-deployment request path**.
The gateway already has domains `acms`, `llm`, `runtime`, `github` (W1–W3).

## Decision

1. **Server Manager is the Proxmox authority; the gateway holds no PVE
   credentials.** The gateway's `proxmox.*` domain is a thin REST adapter
   (`SandboxClient`) against the SM machine API (:8300), which already owns
   the audited allowlisted PVE shim and the ARM provisioning state machine.
   No second PVE integration surface is created.

2. **A sandbox IS an ARM runtime** with `runtime_class="sandbox"` — created
   through the existing ARM provisioning path (placement policy, hygiene
   gate, ownership markers). Sandbox-ness is expressed as:
   - `runtime_class = "sandbox"`
   - `ownership_meta = {kind: "sandbox", ttl_hours: N}`
   - `sandbox_expires_at` timestamp (migration 0004)

3. **TTL is enforced by the ARM reconciler, not the gateway.**
   `reconcile_all()` first runs `expire_stale_sandboxes()`: expired sandboxes
   flip to `desired_state=DESIRED_DESTROYED` + a durable
   `sandbox_ttl_expired` RuntimeEvent. The flip is API-only — the existing
   ownership-verified destroy semantics govern actual teardown; nothing is
   ever auto-deleted from PVE by the sweep itself.

4. **Ownership is the only write authority.** A sandbox's name is
   deterministic: `sbx-<work_uid>-<agent_name>` (lowercased, DNS-safe). Every
   lifecycle tool (`start`/`stop`/`extend_ttl`/`destroy_own`) resolves the
   caller's OWN sandbox by that name from the ARM fleet list; anything else
   → `OUT_OF_SCOPE`/`NOT_FOUND` (never reachable, not merely denied).

5. **Risk classes (§16):** `proxmox.sandbox.status` = READ (own only for
   workers); `proxmox.vm.status` fleet view = READ but role-gated to
   `executive`/`infrastructure_admin`; all lifecycle tools + `deployment.request`
   = SENSITIVE_WRITE (one-time TTL-bounded grants via the W2 approval store).

6. **Protected infrastructure (§26):** no tool accepts a node/target argument
   that could reach MIAM-00133, MIAM-00147, LLDAP, or PBS guests. Worker
   sandbox placement goes through SM's placement policy as code (node
   exclusions are enforced there); a test pins that no tool call can even
   carry a node hint for those nodes.

7. **Merges/deletes of persistent runtimes, org-level operations, and generic
   root-shell MCP tools DO NOT EXIST** in the registry (§26 "do not expose
   generic Proxmox root shell"; §16 DESTRUCTIVE = deny for ordinary workers).

8. **Production deployments** go through `proxmox.deployment.request` →
   durable approval (executive decision) → Server Manager validation
   (target, critical-node protection, backup, rollback, ownership/policy,
   resource constraints) → narrow deployment path. Ordinary workers never
   receive cluster-root access.

## Consequences

- The gateway's trust surface stays free of hypervisor credentials.
- Sandbox provisioning reuses the proven ARM chain (hygiene gate, placement
  policy, ownership markers) instead of a parallel stack.
- TTL expiry is auditable and reversible-by-policy (extend before expiry;
  DESIRED_DESTROYED flip afterwards is still a deliberate teardown step).
- If ARM is down, `proxmox.*` fails honestly (DOMAIN_UNAVAILABLE) — the
  gateway never degrades into a fake success.

## Alternatives considered

- **Direct PVE API from the gateway:** rejected — duplicates credentials,
  audit, ownership logic; wider blast radius.
- **Gateway-side TTL reaper:** rejected — duplicating reconciler duties
  across processes risks split-brain; the ARM reconciler is single-replica.
- **Docker-only sandboxes (no VMs):** deferred — plan §26 explicitly names
  VMs; container isolation can layer later without contract changes.

## References

- Plan §26 (sandbox + deployment), §16 (risk classes), §31 (security tests)
- ADR-0021 (risk classes/approval policy), ADR-0020 (token model)
- Migration `0004_sandbox_ttl`; SM endpoints `/agent-runtimes`,
  `/agent-runtimes/{id}/extend-ttl`, `/reconcile`
