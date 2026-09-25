# ACMS Initial Ideas and Source-Derived Requirement Research

This document is a one-time research compilation. It preserves the provenance IDs requested during design discovery. These entries are **not approved ACMS requirements by themselves**. Priority 3 concepts that were accepted by the human product owner have been normalized into permanent `ACMS-REQ-###` entries in `REQUIREMENTS.md`. Priority 2/1 concepts remain in `FUTURE_WORK.md` until approved.

## Fields

- **Source Status:** Implemented, Documented, Planned, or Derived.
- **Feature Set:** ACMS capability area.
- **Sprint Priority:** 3 core, 2 needed for some features, 1 low-priority/extra.
- **ACMS Mapping:** canonical ACMS requirement or future-work item when one exists.

---

## IBM watsonx Orchestrate ideas

### WAT-REQ-001 - Centralized agent control plane
- **Feature Set:** Workforce Operations
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** A management system should provide centralized visibility, governance, and operational control across agents regardless of where they were built or run.
- **ACMS Mapping:** ACMS-REQ-046, ACMS-REQ-050

### WAT-REQ-002 - Policy and guardrail enforcement
- **Feature Set:** Governance
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** The system should enforce human-approved operating policies, budgets, permissions, and approval gates while agents execute.
- **ACMS Mapping:** ACMS-REQ-026, ACMS-REQ-027, ACMS-REQ-043

### WAT-REQ-003 - Observe and optimize agent performance/cost
- **Feature Set:** Metrics and Improvement
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** The control plane should expose real-time operational visibility, performance, usage, cost, and improvement signals.
- **ACMS Mapping:** ACMS-REQ-041 through ACMS-REQ-045

### WAT-REQ-004 - Human-in-the-loop approvals
- **Feature Set:** Human Control
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Long-running workflows should be able to pause for human validation and resume with audit-ready traceability.
- **ACMS Mapping:** ACMS-REQ-026 through ACMS-REQ-029

### WAT-REQ-005 - Govern heterogeneous/external agents uniformly
- **Feature Set:** Interoperability
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Native, imported, and external A2A agents should be governable through one control plane without forcing them to share one implementation framework.
- **ACMS Mapping:** ACMS-REQ-003, ACMS-REQ-031

### WAT-REQ-006 - Native agent creation/deployment
- **Feature Set:** Agent Instantiation
- **Sprint Priority:** 1
- **Source Status:** Documented
- **Idea:** Some orchestration platforms also build and deploy agents.
- **ACMS Mapping:** FW-011 (deliberately out of ACMS scope)

---

## Cloudroom Core ideas

### CLDGIT-REQ-001 - Durable session recovery
- **Feature Set:** Reliability and Context
- **Sprint Priority:** 3
- **Source Status:** Implemented
- **Idea:** Preserve enough agent/session state to recover eligible work after control-plane interruption without silently creating unrelated replacement conversations.
- **ACMS Mapping:** ACMS-REQ-001, ACMS-REQ-021, ACMS-REQ-035

### CLDGIT-REQ-002 - Harness abstraction
- **Feature Set:** Agent Bridge
- **Sprint Priority:** 3
- **Source Status:** Implemented
- **Idea:** Different coding harnesses should be managed behind a common runtime interface.
- **ACMS Mapping:** ACMS-REQ-003, ACMS-REQ-004

### CLDGIT-REQ-003 - Distinguish lifecycle/work states
- **Feature Set:** Workforce Operations
- **Sprint Priority:** 3
- **Source Status:** Implemented
- **Idea:** The management layer should distinguish queued, active, waiting, completed, failed, interrupted, suspended, and uncertain states.
- **ACMS Mapping:** ACMS-REQ-025, ACMS-REQ-046

### CLDGIT-REQ-004 - Bounded management telemetry
- **Feature Set:** Security and Observability
- **Sprint Priority:** 3
- **Source Status:** Implemented
- **Idea:** Management telemetry should expose health/status without leaking prompts, secrets, protected paths, or sensitive runtime details.
- **ACMS Mapping:** ACMS-REQ-022, ACMS-REQ-025, ACMS-REQ-038, ACMS-REQ-039

### CLDGIT-REQ-005 - Recovery without unsafe replay
- **Feature Set:** Reliability
- **Sprint Priority:** 3
- **Source Status:** Implemented
- **Idea:** Uncertain external actions should be surfaced/reconciled instead of blindly replayed after restart.
- **ACMS Mapping:** ACMS-REQ-034, ACMS-REQ-035

### CLDGIT-REQ-006 - Central inference manager inside orchestration service
- **Feature Set:** Inference Infrastructure
- **Sprint Priority:** 1
- **Source Status:** Planned/architecture-derived
- **Idea:** Some architectures place inference routing directly inside the agent runtime/control service.
- **ACMS Mapping:** Deliberately rejected for MVP; LLM Manager owns inference infrastructure.

---

## Agent OS ideas

### AGENTOS-REQ-001 - Persistent product/project context
- **Feature Set:** Context
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Preserve product mission, roadmap, technical constraints, and project context independently from any single agent session.
- **ACMS Mapping:** ACMS-REQ-017 through ACMS-REQ-020

### AGENTOS-REQ-002 - Standards/context indexing
- **Feature Set:** Context
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** Index reusable standards/context so agents can identify relevant material without loading the entire corpus.
- **ACMS Mapping:** ACMS-REQ-019, ACMS-REQ-020

### AGENTOS-REQ-003 - Structured feature/spec shaping
- **Feature Set:** Work Planning
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** Record scope, decisions, references, constraints, applicable standards, and implementation planning before significant work.
- **ACMS Mapping:** ACMS-REQ-007, ACMS-REQ-011, ACMS-REQ-013

### AGENTOS-REQ-004 - Human authority over scope
- **Feature Set:** Governance
- **Sprint Priority:** 3
- **Source Status:** Derived
- **Idea:** Agents may help shape plans but approved product scope remains human-governed.
- **ACMS Mapping:** ACMS-REQ-010, ACMS-REQ-011

### AGENTOS-REQ-005 - Semantic/vector standards retrieval
- **Feature Set:** Context
- **Sprint Priority:** 2
- **Source Status:** Derived
- **Idea:** More advanced semantic retrieval may improve context selection as corpus size grows.
- **ACMS Mapping:** FW-001

---

## AssistantHub ideas

### HUB-REQ-001 - One management plane for many execution hosts
- **Feature Set:** Workforce Operations
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** A single management experience should supervise agents running across multiple machines without requiring code/credentials to live centrally.
- **ACMS Mapping:** ACMS-REQ-046, ACMS-REQ-050

### HUB-REQ-002 - Durable tasks instead of disposable chats
- **Feature Set:** Work Planning
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Agent work should be represented as durable tasks/work items with history, state, and artifacts rather than only as transient chat threads.
- **ACMS Mapping:** ACMS-REQ-007, ACMS-REQ-036

### HUB-REQ-003 - Human attention inbox/checkpoints
- **Feature Set:** Human Control
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Blocked decisions and checkpoint requests should be collected in a dedicated human-attention workflow.
- **ACMS Mapping:** ACMS-REQ-027, ACMS-REQ-028

### HUB-REQ-004 - Usage/cost reporting
- **Feature Set:** Metrics and Cost
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Show usage/cost by relevant project/agent/model/time dimensions.
- **ACMS Mapping:** ACMS-REQ-041, ACMS-REQ-042

### HUB-REQ-005 - Recurring routines
- **Feature Set:** Automation
- **Sprint Priority:** 3
- **Source Status:** Documented
- **Idea:** Agents can have recurring autonomous work separate from the main interactive task.
- **ACMS Mapping:** ACMS-REQ-014

### HUB-REQ-006 - Full IDE/database/browser/toolchain integration
- **Feature Set:** Developer Tooling
- **Sprint Priority:** 1
- **Source Status:** Documented
- **Idea:** Integrate broad IDE and development-environment features into the management product.
- **ACMS Mapping:** FW-010

---

## CrewAI ideas

### CREWAI-REQ-001 - Role-based collaborative agents
- **Feature Set:** Workforce Orchestration
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** Specialized agents with distinct roles/goals can collaborate on larger objectives.
- **ACMS Mapping:** ACMS-REQ-008, ACMS-REQ-010, ACMS-REQ-015

### CREWAI-REQ-002 - Hierarchical manager coordination
- **Feature Set:** Workforce Orchestration
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** A manager may coordinate task delegation and validate subordinate results.
- **ACMS Mapping:** ACMS-REQ-010, ACMS-REQ-016

### CREWAI-REQ-003 - Event-driven controlled flows
- **Feature Set:** Orchestration
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** Combine autonomous agents with explicit state, events, conditional routing, and deterministic control points.
- **ACMS Mapping:** ACMS-REQ-009, ACMS-REQ-025 through ACMS-REQ-028

### CREWAI-REQ-004 - Human input/checkpointing
- **Feature Set:** Human Control
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** Agent workflows can request human input and resume after review.
- **ACMS Mapping:** ACMS-REQ-028, ACMS-REQ-029

### CREWAI-REQ-005 - Agent memory/knowledge/tools as configurable capabilities
- **Feature Set:** Agent Identity/Context
- **Sprint Priority:** 3
- **Source Status:** Implemented/documented
- **Idea:** Agent identity/configuration can include role, tools, knowledge, memory, model, and behavior settings.
- **ACMS Mapping:** ACMS-REQ-006, ACMS-REQ-017, ACMS-REQ-045

### CREWAI-REQ-006 - ACMS instantiates crews/agents directly
- **Feature Set:** Agent Instantiation
- **Sprint Priority:** 1
- **Source Status:** Derived
- **Idea:** ACMS could create execution agents/crews itself.
- **ACMS Mapping:** FW-011; deliberately outside current ACMS boundary.

---

## Design assumptions used to resolve conflicts

1. **ACMS owns orchestration; LLM Manager owns instantiation/inference infrastructure.**
2. **ACMS is authoritative for approved assignment/governance but not every low-level action.** This avoids turning the control plane into an execution bottleneck.
3. **A2A is the semantic protocol foundation; ACMS extensions fill orchestration gaps.** Do not fork the standard unless necessary.
4. **One primary work assignment does not prohibit local/background routines.** ACMS inventories them but need not schedule each invocation.
5. **A management work item is not necessarily one A2A Task.** A work item can span retries, restarts, review tasks, and multiple A2A executions.
6. **Significant semantic events are durable; token-level streams are not the audit database.** Raw transcripts and Markdown handoffs provide deeper forensic detail.
7. **Human approvals protect boundaries, not every action.** Reversible in-scope work can continue under documented fallback assumptions; mandatory gated actions do not auto-approve on timeout.
8. **External/customer agents are a separate trust domain.** They use an External Agent Gateway and must not directly share internal organizational context.
