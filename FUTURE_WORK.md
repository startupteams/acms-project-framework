# ACMS Future Work

This file captures **unapproved ideas, deferred scope, enhancements, and possible future ACMS requirements**.

Items here are not commitments. Human approval is required before implementation. When approved, promote an item into `REQUIREMENTS.md` with a permanent `ACMS-REQ-###` ID and add it to an appropriate sprint.

## Provenance

- **Human-Directed** - explicitly requested by a human.
- **Agent-Discovered** - proposed by an AI agent during work.
- **Source-Derived** - imported from product/repository research.

---

## FW-001 - Semantic/vector retrieval for durable context

**Proposed requirement:** ACMS-REQ-FUTURE-001  
**Sprint Priority:** 2  
**Provenance:** Human-Directed

**Idea**

Add semantic/vector retrieval as an optional context-selection layer after the Markdown-first context model is proven.

**Why it may matter**

As project/context volume grows, semantic retrieval may improve relevance without loading entire corpora into agent prompts.

**Dependencies / guardrails**

- Markdown remains inspectable source material.
- Retrieval must respect context validity, tenant boundaries, and protected-context permissions.

---

## FW-002 - Dedicated mobile UX

**Proposed requirement:** ACMS-REQ-FUTURE-002  
**Sprint Priority:** 2  
**Provenance:** Human-Directed

Create a deliberately optimized mobile management experience. MVP only requires the web UI to remain usable/accessible from mobile browsers.

---

## FW-003 - Additional A2A transport bindings

**Proposed requirement:** ACMS-REQ-FUTURE-003  
**Sprint Priority:** 2  
**Provenance:** Human-Directed / Source-Derived

Add additional A2A bindings such as gRPC if measured scale, latency, or deployment needs justify them. Do not add multiple transports to MVP merely for theoretical scalability.

---

## FW-004 - Internal event/message bus

**Proposed requirement:** ACMS-REQ-FUTURE-004  
**Sprint Priority:** 2  
**Provenance:** Human-Directed

Introduce an internal durable event bus between agent-ingest, state processing, metrics, notifications, audit, and browser fan-out if ACMS service/database throughput becomes a bottleneck at larger fleet scale.

---

## FW-005 - Production/customer business-agent classes

**Proposed requirement:** ACMS-REQ-FUTURE-005  
**Sprint Priority:** 2  
**Provenance:** Human-Directed

Add product-specific agent classes and governance profiles for production business agents such as AgentifyMe Speed-to-Lead agents.

---

## FW-006 - Customer-specific external roles and governance profiles

**Proposed requirement:** ACMS-REQ-FUTURE-006  
**Sprint Priority:** 2  
**Provenance:** Human-Directed

Define additional roles/policies for customer-facing agent classes, including product-specific data retention, permissions, audit, and tool restrictions.

---

## FW-007 - Automated agent A/B evaluation

**Proposed requirement:** ACMS-REQ-FUTURE-007  
**Sprint Priority:** 2  
**Provenance:** Human-Directed / Source-Derived

Run controlled evaluation workloads across multiple versions/models and compare quality, cost, latency, and scope adherence before promoting a new agent version.

---

## FW-008 - Automated model-reviewer selection service

**Proposed requirement:** ACMS-REQ-FUTURE-008  
**Sprint Priority:** 2  
**Provenance:** Human-Directed

Expand peer review into an automated reviewer-selection service using maintained task-domain benchmark data, cost limits, availability, and independence requirements.

---

## FW-009 - Cryptographic Agent Card signing and mTLS

**Proposed requirement:** ACMS-REQ-FUTURE-009  
**Sprint Priority:** 2  
**Provenance:** Agent-Discovered / Source-Derived

Evaluate signed Agent Cards and mutual TLS (mTLS) for stronger machine-to-machine identity as external/customer deployments and security requirements mature.

**Notes**

mTLS means both sides of a TLS connection present and validate certificates, so ACMS proves its identity to the agent and the agent proves its identity to ACMS at the transport layer. It is stronger machine authentication but introduces certificate issuance, rotation, revocation, expiry, and operational complexity.

---

## FW-010 - Browser/devtools/database/toolchain IDE features

**Proposed requirement:** ACMS-REQ-FUTURE-010  
**Sprint Priority:** 1  
**Provenance:** Source-Derived (AssistantHub)

General-purpose IDE/database/browser/toolchain features may be reconsidered later but are not core to the ACMS orchestration mission.

---

## FW-011 - Native agent-instantiation workflows

**Proposed requirement:** ACMS-REQ-FUTURE-011  
**Sprint Priority:** 1  
**Provenance:** Source-Derived (IBM/CrewAI style platforms)

Some source platforms build/deploy agents directly. ACMS intentionally delegates instantiation to LLM Manager and approved provisioning systems. Reconsider only if the architectural boundary changes by human decision.

---

## FW-012 - Full token-level stream retention

**Proposed requirement:** ACMS-REQ-FUTURE-012  
**Sprint Priority:** 1  
**Provenance:** Agent-Discovered

Retain every token/event fragment only if a concrete forensic/compliance need justifies the cost and privacy exposure. MVP retains raw transcripts plus significant semantic events instead.


---

## FW-013 - Rich React/Next.js dashboard frontend

**Proposed requirement:** ACMS-REQ-FUTURE-013  
**Sprint Priority:** 2  
**Provenance:** Human-Directed (deployment plan §5)

Replace the temporary server-rendered Jinja2 UI (ADR-0008) with a proper SPA frontend consuming the ACMS API, once backend features (assignments, status, heartbeat, cost) make richer views worthwhile.

**Dependencies / guardrails**

- The Jinja2 UI remains the control surface until the SPA reaches parity on read views.
- The SPA must never handle the machine bearer token; human auth stays LLDAP/session-cookie based.

---

## FW-014 - Privileged UI actions with full authorization rules

**Proposed requirement:** ACMS-REQ-FUTURE-014  
**Sprint Priority:** 2  
**Provenance:** Human-Directed (deployment plan §6)

Add Administrator action buttons (e.g. deregister agent, rotate token) to the UI only after backend operations and per-role authorization rules exist, with audit records for each action (ACMS-REQ-025).

**Dependencies / guardrails**

- Requires ACMS-REQ-025 audit backend.
- Observer/Worker must never see privileged controls.
