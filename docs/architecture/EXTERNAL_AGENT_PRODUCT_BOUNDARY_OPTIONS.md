# External-Agent Product Boundary Options (Phase F3, 2026-09-29)

**Premise:** "a gateway is more than a firewall." Whatever shape is chosen, SOMETHING must own:

| Concern | Why it can't just be nginx |
|---|---|
| Authentication | tenant API keys / OAuth — issued, rotated, audited by product code |
| Tenant identity & isolation | mapping tenant → data/agent/config with deny-by-default |
| Rate limits & spend limits | per-tenant caps (LLM Manager §9.3 pattern: caps BEFORE any cloud spend) |
| Payload validation | schema-bound requests; prompt-size bounds; injection screening |
| Tool allowlists | which tools the external agent may invoke at all |
| Customer/client guardrails | forbidden actions (no internal hosts, no cross-tenant reads) |
| Auditing | per-tenant durable event trail (reconstruct any session) |
| A2A adaptation | bridging product requests to agent-run invocations |

## Options

### A. Product Backend/BFF → ACMS adapter
Speed-to-Lead's own backend (the portal's server) fronts everything; it invokes ACMS (or directly
Hermes runs) per tenant.
- Isolation via: tenant rows in the product DB; ACMS stays internal-only (never tenant-facing).
- Pros: simplest; product owns its domain logic; ACMS keeps internal-only surface (AGENTS.md
  security rule: internal bridge endpoints stay private).
- Cons: every tenant safeguard is hand-rolled in the product; easy to scatter enforcement.

### B. Dedicated External Agent Gateway (a real service)
A standalone service whose ONLY job is the table above: tenant auth, isolation, caps, validation,
allowlists, audit, and A2A adaptation into agent runtimes (Hermes api-server).
- Pros: one enforce-point (hard to bypass); reusable for future external agent classes beyond
  Speed-to-Lead; ACMS and product both stay internal; audit is centralized.
- Cons: new service to build/operate; risk of becoming a second control plane if scope creeps.

### C. Edge API gateway + product service + ACMS adapter
Off-the-shelf edge gateway (nginx/Kong/Traefik style: TLS, IP, coarse rate limits) IN FRONT OF the
product backend, which uses B-style internals or A.
- Pros: layered defense (edge handles transport/coarse limits; product handles tenant logic).
- Cons: edge gateways do NOT understand tenants' agent semantics; the dangerous logic (tool
  allowlists, guardrails) still needs B-style code somewhere — the edge adds ops surface, not
  enforcement of the hard parts.

### D. Public multi-tenant ACMS API
Open ACMS to tenants directly.
- **Rejected** — inconsistent with the recorded direction (ACMS = internal infrastructure) and
  with AGENTS.md ("external/customer agents are a separate trust domain"). Would require re-audit
  of every ACMS surface for multi-tenant isolation.

## Recommendation

**B as the enforce-point, fronted by C's edge layer when public traffic exists:**
- The **External Agent Gateway** is the single enforce-point for tenant auth/isolation/caps/
  allowlists/audit/A2A adaptation.
- Speed-to-Lead's product backend talks TO the gateway (server-to-server, private), and the portal
  UI talks to the product backend — tenants never see ACMS or raw agent endpoints.
- Edge gateway (nginx, already the house pattern) handles TLS/coarse limits in front later; it is
  transport, not policy.

## Placement of each concern (the chosen shape)

```text
Tenant/Portal UI ──► Speed-to-Lead Product Backend ──► External Agent Gateway ──► Agent runtime(s)
                        tenant data, lead logic          authn, tenant identity,
                        billing, human UI               rate/spend caps, payload
                                                        validation, tool allowlist,
                                                        guardrails, audit, A2A adapt
```

- **Authentication:** gateway (tenant keys), product backend (human portal login).
- **Tenant isolation:** gateway (agent/config namespaces) + product DB (lead data).
- **Rate/spend:** gateway (per-tenant LLM spend caps — caps-first rule), edge (coarse QPS).
- **Payload validation:** gateway (schemas, size, injection screening) + product (domain rules).
- **Tool allowlists:** gateway→runtime config per tenant class; deny-by-default.
- **Auditing:** gateway (durable, per-tenant, correlated ids); product (business events).

## Threat model & evidence-oriented test plan (F4)

Top assumption to falsify: **"the boundary actually isolates customers and enforces guardrails."**

| Threat | Test (staging sandbox only — never production customer systems) | Pass criterion |
|---|---|---|
| Cross-tenant data access | Tenant-A agent prompted to fetch Tenant-B lead data by id/guessing | zero rows; deny audited |
| Credential misuse | Reuse tenant-A key against tenant-B endpoint / replay / scope escalation | 401/403 every path |
| Tool outside allowlist | Agent attempts tool not in tenant allowlist (e.g. terminal, file) | blocked at gateway + audited |
| Prompt/policy bypass | Prompt-injection attempts to disable guardrails ("ignore instructions") | guardrails hold; attempt logged |
| Data exfiltration | Agent tries external egress (webhooks, pastebin, unknown domains) | egress denied/allowlisted |
| Rate/spend abuse | Burst + runaway loops | caps trip; agent paused; spend ledger matches caps |
| Wrong-customer routing | Craft requests referencing another tenant's session/agent ids | 404/403; no cross-session state |
| External agent compromise | Assume agent host compromised: what can it reach? | blast radius = its tenant namespace only |
| Audit reconstruction | After a simulated incident, rebuild who-did-what from audit alone | complete per-tenant timeline |
| Internal-state leakage | Inspect external agent's profile/config for internal markers | zero internal memories/skills/hosts |

Run this suite in the staging sandbox before ANY real tenant; re-run on every gateway release.
