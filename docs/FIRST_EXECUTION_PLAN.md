# ACMS First Execution Plan

**Date:** 2026-09-25

## Goal

Bootstrap the ACMS repository from the provided framework, then begin implementation through small human-reviewed pull requests. The initial direct-to-main action is limited to importing/normalizing the project framework and approved design documentation. All feature implementation after bootstrap uses branches and pull requests.

## Phase 0 - Bootstrap repository documentation (direct to main, human-authorized)

Apply the contents of this package to `startupteams/acms-project-framework`:

- `README.md`
- `REQUIREMENTS.md`
- `FUTURE_WORK.md`
- `INITIAL_IDEAS.md`
- `SPRINT.md`
- `AGENTS.md`
- `docs/BUSINESS_PROBLEMS.md`
- `docs/ARCHITECTURE.md`
- `docs/LLM_MANAGER_DIRECTION.md`
- `docs/FIRST_EXECUTION_PLAN.md`
- accepted ADRs in `docs/adr/`

Preserve existing framework templates in `docs/adr/TEMPLATE.md`, `docs/tdr/TEMPLATE.md`, `docs/AGENT_READINESS.md`, and `.github/PULL_REQUEST_TEMPLATE.md`, updating only ACMS-specific wording when appropriate.

### Bootstrap validation

- Confirm all required files exist.
- Confirm all `ACMS-REQ-###` IDs are unique.
- Confirm every Priority 3 source concept that was approved maps to an ACMS requirement.
- Confirm Priority 2/1 work remains in `FUTURE_WORK.md`.
- Confirm Mermaid blocks render syntactically.
- Confirm no credentials/secrets are committed.

## Phase 1 - First implementation PR: runtime stack + ACMS service scaffold

**Branch:** `feat/ACMS-001-control-plane-scaffold`

**Primary requirements:** `ACMS-REQ-001`, `ACMS-REQ-003`, `ACMS-REQ-031`, `ACMS-REQ-038`, `ACMS-REQ-039`

### Intent

Create the smallest backend service capable of becoming the ACMS registry/control plane and prove the A2A/Agent Bridge integration path.

### Human review gate

The implementation agent must first add a **Proposed ADR** selecting the concrete MVP technology stack and authentication approach. Because the stack is a significant architecture decision not yet explicitly chosen by the human, do not mark it Accepted and do not merge until human review.

### Recommended candidate for the Proposed ADR

Use a simple agent-friendly stack unless repository/environment evidence argues otherwise:

- backend: Python 3.12 + FastAPI;
- schema/validation: Pydantic;
- persistence abstraction: SQLAlchemy with PostgreSQL target;
- migrations: Alembic;
- tests: pytest;
- protocol adapter: current maintained A2A Python SDK/compatible implementation;
- API/event layer: HTTP + SSE;
- frontend deferred to a later PR after backend contracts stabilize.

This is a recommendation for the first PR, **not an accepted architecture decision**.

### Deliverables

- backend project skeleton;
- health/readiness endpoint;
- version endpoint;
- configuration loading with secrets excluded from source;
- initial Agent Registry domain model;
- Agent Card/capability model adapter;
- authentication interface with a minimal secure implementation selected in the Proposed ADR;
- deterministic tests;
- setup/run/test commands documented in README;
- Proposed ADR for stack/auth choice;
- handoff file.

### Definition of done

- tests pass;
- service can start locally;
- one sample agent can be represented in the registry model;
- unsupported capabilities are representable;
- no actual harness control is required yet;
- human reviewer approves the ADR and PR before merge.

## Phase 2 - PR: Agent registration and capability discovery

**Branch:** `feat/ACMS-003-agent-registry`

**Requirements:** `ACMS-REQ-001` through `ACMS-REQ-004`, `ACMS-REQ-024`, `ACMS-REQ-039`

### Deliverables

- persistent agent registry;
- Internal/External trust classification;
- Agent Bridge registration endpoint/workflow;
- Agent Card/capability version/hash tracking;
- refresh/re-registration semantics;
- authorization checks;
- tests for duplicate identity, stale card, unauthorized registration/control.

## Phase 3 - PR: Work items and one-primary-assignment rule

**Branch:** `feat/ACMS-007-work-orchestration`

**Requirements:** `ACMS-REQ-007` through `ACMS-REQ-014`, `ACMS-REQ-036`

### Deliverables

- Product/Project/Feature/Work Item minimal domain model;
- one-primary-assignment invariant;
- work-item to one-or-more A2A task/run relationships;
- background-routine inventory;
- Markdown handoff linkage;
- scope/provenance fields for Future Work discoveries.

## Phase 4 - PR: A2A bridge control + SSE + heartbeat

**Branch:** `feat/ACMS-031-a2a-control`

**Requirements:** `ACMS-REQ-005`, `ACMS-REQ-031` through `ACMS-REQ-037`

### Deliverables

- A2A-based send/steer/control adapter;
- SSE ingestion for task/status/artifact updates;
- ACMS heartbeat extension/profile;
- stale-agent state machine;
- active reconciliation query;
- daily fleet reconciliation job;
- significant-event sequencing/correlation;
- tests for dropped stream, missed heartbeat, reconnect, unsupported capability, and steering latency.

## Phase 5 - PR: Audit, approvals, and human-attention queue

**Branch:** `feat/ACMS-023-governance`

**Requirements:** `ACMS-REQ-023` through `ACMS-REQ-030`

### Deliverables

- human roles: Administrator, Worker, Observer;
- append-oriented audit records;
- mandatory approval classes;
- timeout/default behavior;
- human-attention queue;
- high-autonomy/sandbox policy;
- direct human-agent message records.

## Phase 6 - PR: Context hierarchy and protection

**Branch:** `feat/ACMS-017-context`

**Requirements:** `ACMS-REQ-017` through `ACMS-REQ-022`

### Deliverables

- context metadata hierarchy;
- Markdown-backed records/references;
- validity states/timestamps;
- protected-context authorization;
- transcript/summary linkage;
- tenant/internal-external isolation tests.

## Phase 7 - PR: Dashboard MVP

**Branch:** `feat/ACMS-046-dashboard`

**Requirements:** `ACMS-REQ-046` through `ACMS-REQ-049`

### Deliverables

- agent fleet status;
- primary assignment and latest direction/progress;
- human-attention view;
- basic Kanban work board;
- direct agent chat/steering panel;
- aggregated UI event stream from ACMS rather than browser-to-agent connections;
- responsive enough for basic mobile browser use.

## Phase 8 - PR: Cost and performance integration

**Branch:** `feat/ACMS-041-cost-performance`

**Requirements:** `ACMS-REQ-015`, `ACMS-REQ-041` through `ACMS-REQ-045`

### Deliverables

- versioned LLM Manager/PDU Manager metrics adapter contract;
- cost/usage ingestion;
- agent/project/work attribution;
- month-end projection and top-spender views;
- performance-to-cost view;
- human feedback and peer-review history;
- reviewer recommendation hook using capability/benchmark evidence when supplied by LLM Manager.

## Phase 9 - PR: External Agent Gateway foundation

**Branch:** `feat/ACMS-040-external-gateway`

**Requirement:** `ACMS-REQ-022`, `ACMS-REQ-024`, `ACMS-REQ-040`

Implement only after internal agent orchestration is stable enough to reuse the same ACMS contracts.

## Pull request rules

Every feature PR must:

1. list the ACMS requirement IDs;
2. state acceptance criteria being implemented;
3. identify architecture/security implications;
4. include validation output;
5. include/update a Markdown handoff;
6. add new out-of-scope discoveries to `FUTURE_WORK.md` with provenance;
7. surface Proposed ADRs for human approval;
8. never self-merge.

## Scaling strategy

Do **not** add an event bus, gRPC, Kubernetes, sharding, or token-stream persistence in advance of evidence.

The first architecture scales by:

- keeping execution local to agents;
- event/heartbeat-driven status rather than constant polling;
- aggregated browser updates;
- semantic-event persistence rather than token-fragment persistence;
- one standard bridge contract.

When load testing or production evidence shows a bottleneck, the expected first scale-out step is to separate ingest/event processing behind an internal message/event bus without changing the agent-facing A2A contract.

## Handoff to implementation agent

After applying Phase 0 to main:

1. read `AGENTS.md`, `REQUIREMENTS.md`, `docs/BUSINESS_PROBLEMS.md`, `docs/ARCHITECTURE.md`, and accepted ADRs;
2. create `feat/ACMS-001-control-plane-scaffold` from current main;
3. draft the stack/auth ADR as **Proposed**;
4. implement only the Phase 1 deliverables;
5. run and record validation;
6. create a Markdown handoff;
7. open a pull request and stop for human verification.
