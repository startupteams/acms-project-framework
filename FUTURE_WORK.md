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

## FW-015 - Feature-flag mechanism for risky agent-control features

**Proposed requirement:** ACMS-REQ-FUTURE-015
**Sprint Priority:** 2
**Provenance:** Human-Directed (feature-delivery plan §25)

Add a simple environment/config-backed feature-flag mechanism before risky
agent-control features (e.g. `ACMS_FEATURE_WORK_UI`, `ACMS_FEATURE_A2A_CONTROL`,
`ACMS_FEATURE_HEARTBEAT`). Flags are a quick disable mechanism, not a
substitute for rollback.

**Dependencies / guardrails**

- Land before A2A control (slice 4) and heartbeat (slice 3) features.
- Flags must fail closed (absent → disabled) and never gate authn/authz.

## FW-016 - Maintenance-window reconciliation and stale-release alarms

**Proposed requirement:** ACMS-REQ-FUTURE-016
**Sprint Priority:** 3
**Provenance:** Agent-Discovered

If a release transaction aborts between maintenance-on and maintenance-off
(e.g. power loss), ACMS stays in maintenance mode with the app stopped. A
watchdog should alert and offer deterministic recovery: if `transaction.json`
is open, resume or roll back automatically; if stale, exit maintenance mode.

**Dependencies / guardrails**

- Requires ADR-0009 release tooling (this PR) deployed first.
- Never auto-restore a DB without the transaction guards (plan §29).

## FW-LLM-LOCAL-COST - Authoritative local-inference USD-equivalent cost

**Proposed requirement:** ACMS-REQ-FUTURE-ECON-1
**Sprint Priority:** 3
**Provenance:** Agent-Discovered (REV2 plan §4.1/§9B.4)

Budget rollups and economics reports currently treat local inference cost as
UNKNOWN (nullable columns / `local_cost_usd=None`), because MARION-hosted
models have no authoritative USD-equivalent accounting. Until LLM Manager
exposes a defensible allocation (energy + amortized hardware + ops), local
usage stays usage-only (`local_compute_seconds`, tokens) and is never blended
into cost-per-accepted metrics. A proposed approach: LLM Manager publishes a
configurable $/GPU-hour allocation rate; ACMS consumes it as telemetry, never
as an invented constant.

**Guardrails**
- Never fabricate a local $ figure to make rollups look complete.
- When the rate arrives, it applies prospectively; historical entries keep
  their UNKNOWN status.

## FW-AB-BAKEOFF - Controlled model A/B bake-off support

**Proposed requirement:** ACMS-REQ-FUTURE-ECON-2
**Sprint Priority:** 3
**Provenance:** Human-Directed (REV2 plan §9B.7)

Schema support for controlled comparisons already exists implicitly
(`pr_outcomes` + `cost_attribution` + `requirement_links` + task_category
filtering). What remains: a small "bake-off spec" record that pins the
comparison envelope — same starting commit, same execution plan, same tool
permissions, same acceptance criteria, same time/budget envelope — and links
the participating outcome rows. All runs including failures are recorded.
Model names are DATA (outcome rows), never hard-coded into the economic model;
the first suggested challenger set (current default cloud worker, one cheaper
challenger, one stronger/higher-cost challenger) is a human decision at
bake-off time.

**Guardrails**
- No automatic heavy benchmark spend; each bake-off needs explicit human approval.
- Report side-by-side cost-per-accepted-requirement with first-pass rates.

## FW-ROUTING-ECON - Economics-aware future model routing

**Proposed requirement:** ACMS-REQ-FUTURE-ECON-3
**Sprint Priority:** 4
**Provenance:** Human-Directed (REV2 plan §9B.8)

Architecture direction (not autonomous routing yet): future model routing
should optimize **historical cost per accepted requirement/PR for similar
work** (task_category + repo + scope similarity), subject to quality,
security, latency, and policy constraints. Raw token price alone must never be
the routing objective. This needs the economics report data to accumulate
first (this PR's instrumentation) plus a routing-policy ADR before any
implementation.

**Guardrails**
- Requires a minimum sample of accepted outcomes per model/task-class before
  routing decisions would be defensible.
- Never route away from a model mid-work-item (stability of the execution context).
