# ADR-0016: Worker placement authority and durable agent identity (MIAM-00100 default)

**Status:** Proposed (human direction: STEA-004 execution plan 2026-10-01 §1.1/§2/§21/§22)
**Date:** 2026-10-01

## Context

The only real worker (acms-worker-001, VM124) runs on miam00111 — a busy
inference node. The plan makes MIAM-00100 the default worker host, forbids
miam00133/miam00147, caps inference-node workers at 1, and requires durable
agent identity that survives migration/recreation (never inferred from
VMID/hostname/node/IP).

## Decision

### Durable identity (already true structurally; made explicit)

- ACMS agent UUID (`22ac1b19-…`) is the ONLY durable identity. Runtime location
  lives in Server Manager/ARM (`agent-runtimes`: node, vmid) as MUTABLE
  metadata. Bridge targets are resolved by agent_id at dispatch time
  (`bridge_discovery` → ARM), so a moved VM needs only the ARM runtime record
  + its own gateway config update — never a new ACMS agent.
- The §8 ownership marker in the VM description carries the binding and MUST
  be preserved through any migration/recreation.
- IP drift: the current fallback env `ACMS_BRIDGE_TARGETS_JSON` pins .203 —
  after migration the ARM discovery path is authoritative; the env fallback
  gets updated to the new IP in the same change window (documented, not
  hand-edited silently).

### Placement policy (Server Manager domain)

- Eligible worker hosts: MIAM-00100 FIRST (default, 62GB free now).
- Never: MIAM-00133, MIAM-00147 (critical services/backup).
- Inference nodes (hosts with a serving model per LLM Manager hosts table):
  max 1 worker runtime each; only when MIAM-00100 capacity is unsuitable or
  model-locality justifies it, and it does not materially harm inference.
- Capacity checks use measured pressure (PVE `pressurecpufull`/
  `pressurememorysome`), not name heuristics.
- ACMS does NOT implement placement rules; it requests workers from
  Server Manager (existing `server_manager_client` contract). Placement
  eligibility lives in ARM provisioning logic + these documented constraints.

### VM124 migration mechanics (plan §22)

Verified constraints (2026-10-01): VM124 disks are `testthin:` (miam00111
local thin) — NOT shared storage; `qm migrate` with `--with-local-disks`
would copy ~200G over the network (slow, feasible). The lower-risk path:
**recreate runtime on MIAM-00100 from template VM121 preserving durable
identity** (plan §22 explicitly authorizes recreation). Steps: prove no
running work → capture Hermes state (the hourly `hermes-state-sync` already
pushes sessions/memories/skills to branch `agent/22ac1b19`) → clone template
to a new VMID on miam-00100 → re-identify (machine-id, host keys, netplan
per clone-hygiene gate) → install/restore Hermes profile with SAME
`acms-worker-001` profile name + api-server key → update ARM runtime record
(node/vmid/ip) via Server Manager → verify heartbeat + A2A + model access.
Old VM124 powers off AFTER verification (rollback anchor), then destroyed.

## Consequences

- A human live-migrating the VM later never creates a new ACMS agent.
- MIAM-00100 carries the worker-capacity threshold question (62GB host RAM:
  a 32GB worker + 8GB CT906 fits; second worker needs memory review).