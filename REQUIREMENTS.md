# AgentifyMe Cloud Management System - Requirements

This file is the source of truth for **approved ACMS product scope**.

Requirements use permanent IDs in the form `ACMS-REQ-###`. There is no system/software split. Each requirement is atomic, implementation-agnostic where possible, and contains its acceptance criteria directly with the requirement.

Source-derived ideas retain their original provenance IDs in `INITIAL_IDEAS.md`. Priority 3 ideas that have been accepted by the human product owner are represented here as ACMS requirements. Priority 2 and 1 ideas remain in `FUTURE_WORK.md` until explicitly promoted.

## Priority definition

- **3 - Core:** required for the primary function of ACMS.
- **2 - Needed for some features:** useful/important but not required for the first usable control plane.
- **1 - Extra:** low-priority, optional, or experimental.

---

## Feature Set: Agent Registry, Identity, and Lifecycle Control

### ACMS-REQ-001 - Persistent agent identity

**Sprint Priority:** 3

**Requirement**

ACMS shall maintain a persistent identity for every registered AI agent that remains stable across agent process restarts, task executions, sessions, model changes, and harness updates unless a human explicitly creates a new identity.

**Acceptance criteria**

- Every registered agent has a unique ACMS agent ID.
- Agent history remains associated with the same identity after restart.
- Session/run identifiers are distinct from the persistent agent identity.

**Verification**

- Register an agent, execute work, restart its process, reconnect it, and verify the same ACMS agent ID and history are retained.

### ACMS-REQ-002 - Orchestrate registered agents without instantiating them

**Sprint Priority:** 3

**Requirement**

ACMS shall orchestrate agents that have been instantiated and registered by LLM Manager or another human-approved provisioning process, and shall not itself provision the underlying VM, container, harness installation, or model-serving infrastructure.

**Acceptance criteria**

- Registration accepts an already-existing agent identity and endpoint/capabilities.
- No ACMS workflow is required to create the underlying execution environment.
- Provisioning failures outside ACMS are surfaced as external dependency failures rather than silently reimplemented by ACMS.

### ACMS-REQ-003 - Standardized ACMS Agent Bridge

**Sprint Priority:** 3

**Requirement**

ACMS shall define a standardized Agent Bridge interface that adapts supported harnesses such as Hermes, Pi, Codex, and future harnesses into a common ACMS management contract.

**Acceptance criteria**

- ACMS can query a bridge for the registered agent identity and supported capabilities.
- Harness-specific control details remain behind the bridge boundary.
- Adding a new harness does not require changing the ACMS work-management domain model.

### ACMS-REQ-004 - Versioned capability advertisement

**Sprint Priority:** 3

**Requirement**

Each registered Agent Bridge shall expose a versioned capability description compatible with the Agent2Agent Agent Card model and ACMS extensions so ACMS can determine which operations the agent supports.

**Acceptance criteria**

- Capability data identifies bridge version, protocol/profile version, and supported management capabilities.
- ACMS does not present an unsupported management action as if it were supported.
- A capability/card version change can be detected and refreshed without creating a new agent identity.

### ACMS-REQ-005 - Agent lifecycle controls

**Sprint Priority:** 3

**Requirement**

ACMS shall allow an authorized human or delegated manager to send supported lifecycle and steering commands to a registered agent, including start, stop, restart, pause, resume, interrupt, cancel, steer, reprioritize, and reassign work.

**Acceptance criteria**

- ACMS exposes only controls supported by the agent capability description.
- Every accepted control command is auditable.
- Unsupported operations return an explicit unsupported result rather than being silently simulated.

### ACMS-REQ-006 - Versioned and lockable agent identity configuration

**Sprint Priority:** 3

**Requirement**

ACMS shall track versions of the durable configuration that defines how an agent is expected to behave and shall allow an approved version to be locked to preserve a repeatable working pipeline.

**Acceptance criteria**

- Agent configuration versions are identifiable and historically traceable.
- A locked version cannot be materially changed without authorized human action.
- Task/session context can continue to evolve without silently changing a locked core agent version.

---

## Feature Set: Work Planning and Orchestration

### ACMS-REQ-007 - Hierarchical work model

**Sprint Priority:** 3

**Requirement**

ACMS shall organize work using a hierarchy capable of representing products, projects, goals, features, requirements, work items/tasks, executions/runs, handoffs, and resulting artifacts.

**Acceptance criteria**

- A work item can be traced to its parent project and, when applicable, feature/requirement.
- Executions and artifacts can be traced back to the work item that produced them.
- The hierarchy does not require every project to use every optional level.

### ACMS-REQ-008 - One primary assignment per agent

**Sprint Priority:** 3

**Requirement**

ACMS shall maintain at most one primary ACMS work assignment for a registered worker agent at a time.

**Acceptance criteria**

- The dashboard can answer what primary work each agent is currently assigned.
- A new primary assignment requires completion, release, suspension, or explicit reassignment of the previous primary assignment.
- Background routines do not count as a second primary assignment.

### ACMS-REQ-009 - Decentralized execution within an approved envelope

**Sprint Priority:** 3

**Requirement**

After assigning approved work, ACMS shall allow an agent to autonomously choose implementation steps inside its authorized scope, budget, permissions, context access, and execution environment without requiring ACMS approval for every tool call, shell command, retry, test, or local action.

**Acceptance criteria**

- Routine reversible execution does not require a round trip to ACMS for permission.
- The agent remains constrained by scope, budget, security, and approval boundaries.
- Crossing a mandatory boundary produces a human-attention/approval event rather than implicit authorization.

### ACMS-REQ-010 - In-scope task decomposition by manager agents

**Sprint Priority:** 3

**Requirement**

ACMS shall permit authorized manager agents to decompose approved product/project/feature scope into subordinate tasks and assign those tasks to available agents without expanding the approved scope.

**Acceptance criteria**

- Manager-created tasks remain traceable to approved parent scope.
- Manager agents cannot silently introduce new product requirements or remove approved scope.
- Proposed scope changes are surfaced to a human through a handoff or co-work request.

### ACMS-REQ-011 - Preserve approved scope during execution

**Sprint Priority:** 3

**Requirement**

ACMS shall treat human-approved feature/project scope as authoritative during execution and shall require human authorization before approved scope is expanded, removed, or moved to future work.

**Acceptance criteria**

- Agents may suggest alternative scope but may not silently replace approved scope.
- When blocked, an agent can preserve state, create a handoff, and request direction.
- Future ideas discovered during execution are recorded separately from approved scope.

### ACMS-REQ-012 - Human-directed and agent-discovered future work provenance

**Sprint Priority:** 3

**Requirement**

ACMS shall distinguish future-work ideas explicitly directed by humans from future-work ideas discovered by agents or imported from source research.

**Acceptance criteria**

- Future-work records contain a provenance classification.
- Human-Directed future work is distinguishable from Agent-Discovered and Source-Derived future work.
- No future-work item becomes approved scope without human promotion.

### ACMS-REQ-013 - Structured Markdown handoffs

**Sprint Priority:** 3

**Requirement**

ACMS shall require meaningful agent work to produce or update a structured Markdown handoff that records the assignment, actions taken, results, validation, current understanding, blockers, assumptions, and recommended next action.

**Acceptance criteria**

- A completed or paused meaningful work item has an associated handoff.
- The handoff distinguishes observed changes/results from the agent's interpretation when relevant.
- Handoffs are linkable from work-item history.

### ACMS-REQ-014 - Background routine inventory

**Sprint Priority:** 3

**Requirement**

ACMS shall allow agents to run background routines such as cron jobs independently of their primary ACMS assignment while maintaining a simple inventory of known enabled routines and their latest reported status.

**Acceptance criteria**

- An agent can have one primary work item and multiple background routines concurrently.
- ACMS records routine name/purpose, schedule or trigger description when available, enabled state, and latest reported outcome/status.
- ACMS is not required to schedule or individually authorize every background routine execution.

### ACMS-REQ-015 - Peer review across agents and models

**Sprint Priority:** 3

**Requirement**

ACMS shall support assigning peer-review work to an agent other than the agent that produced the work and shall support selecting reviewers using task-relevant model/agent capability evidence when available.

**Acceptance criteria**

- Work can be routed to a distinct reviewer agent.
- When comparative benchmark/capability data exists for the task domain, ACMS can prefer a reviewer whose model is stronger for that domain.
- If no demonstrably stronger model is available, ACMS can use a different agent/model for independent review.
- Review outcomes are recorded in the producer agent's performance history and work-item audit trail.

### ACMS-REQ-016 - Executive agent delegation

**Sprint Priority:** 3

**Requirement**

ACMS shall allow a human to designate an Executive Agent that uses the same standard agent communication interface as other agents but may receive broader explicitly delegated authority and context to coordinate subordinate agents on the human's behalf.

**Acceptance criteria**

- Executive authority is represented as permissions/delegation rather than a separate transport protocol.
- The human can interact primarily with the Executive Agent while subordinate actions remain auditable in ACMS.
- Mandatory non-delegable controls, such as approved budget ceilings, remain enforced unless a human explicitly changes the underlying policy.

---

## Feature Set: Context and Memory Management

### ACMS-REQ-017 - Hierarchical context model

**Sprint Priority:** 3

**Requirement**

ACMS shall represent durable context in distinct Organization, Product, Project, Feature, Agent, Task, and Live Session levels.

**Acceptance criteria**

- Context records identify their level and owning scope.
- Lower-level context can reference applicable higher-level context without copying all content into every record.
- Task context may span multiple runtime sessions; Live Session context represents the current execution/conversation state.

### ACMS-REQ-018 - Protected high-level context

**Sprint Priority:** 3

**Requirement**

ACMS shall protect Organization, Product, Project, and Feature context from autonomous modification unless a human explicitly authorizes the change or delegates that authority.

**Acceptance criteria**

- Agent-generated changes to protected context are proposed rather than silently applied by default.
- Human authorization/delegation is auditable.
- Agent, Task, and Live Session context may be modified within assigned policy without the same default approval gate.

### ACMS-REQ-019 - Context validity and lifecycle state

**Sprint Priority:** 3

**Requirement**

ACMS shall assign explicit lifecycle state and timestamps to durable context so obsolete information is not treated as current solely because it is retrievable.

**Acceptance criteria**

- Supported states include Current, Superseded, Draft, Proposed, and Archived.
- Context records include created and last-modified timestamps.
- Retrieval/selection logic can distinguish old but explicitly Current durable standards from old superseded material.

### ACMS-REQ-020 - Markdown-first durable context

**Sprint Priority:** 3

**Requirement**

ACMS shall support human-readable Markdown as the initial durable representation for plans, handoffs, requirements, architectural intent, and structured contextual records that humans must be able to inspect and version.

**Acceptance criteria**

- Core durable human-facing context can be exported/read as Markdown.
- Markdown records can be version-controlled.
- The MVP does not require vector storage to function correctly.

### ACMS-REQ-021 - Preserve raw transcripts and structured summaries

**Sprint Priority:** 3

**Requirement**

ACMS shall preserve raw agent-session transcripts when available and shall separately preserve structured summaries/handoffs so humans can compare what actually occurred with the agent's interpretation of the work.

**Acceptance criteria**

- Raw transcript and structured summary are separately identifiable.
- Retained transcript data is associated with the correct agent/run/work item.
- A handoff can be inspected without requiring replay of the entire transcript.

### ACMS-REQ-022 - Internal/external context isolation

**Sprint Priority:** 3

**Requirement**

ACMS shall prevent external/customer agents from receiving internal organizational context or unrelated customer context unless an explicit authorized policy permits the specific disclosure.

**Acceptance criteria**

- Agents have an Internal or External trust classification.
- External agents have a tenant/customer scope.
- Context retrieval enforces the classification/scope boundary.

---

## Feature Set: Human Control, Governance, and Audit

### ACMS-REQ-023 - Human roles

**Sprint Priority:** 3

**Requirement**

ACMS shall support at least three human access roles: Administrator, Worker, and Observer.

**Acceptance criteria**

- Administrators can perform administrative and policy actions allowed by system policy.
- Workers have limited operational access appropriate to assigned work.
- Observers can view permitted status/history without modifying controlled state.

### ACMS-REQ-024 - Agent trust classification

**Sprint Priority:** 3

**Requirement**

ACMS shall classify registered agents as Internal or External and shall allow governance rules to differ by trust classification and, for External agents, by customer/product class.

**Acceptance criteria**

- Trust classification is visible in the registry and audit trail.
- External classification applies stricter context/traffic isolation by default.
- Future external bot classes can define additional rules without changing the Internal agent model.

### ACMS-REQ-025 - Audit significant human and agent actions

**Sprint Priority:** 3

**Requirement**

ACMS shall maintain an append-oriented audit history of significant human commands, agent decisions, major agent actions, state transitions, approvals, scope/budget events, errors, handoffs, and artifacts.

**Acceptance criteria**

- Audit records include actor, timestamp, target, event type, and trace/correlation identifiers where available.
- Audit history distinguishes human actions from agent actions.
- Fine-grained token-stream fragments are not required to be durable audit records.

### ACMS-REQ-026 - Mandatory human approval boundaries

**Sprint Priority:** 3

**Requirement**

ACMS shall require explicit human approval before an agent exceeds an approved budget, expands approved scope, changes protected credentials/permissions, performs destructive infrastructure operations, deploys to production, deletes durable protected content, or materially modifies protected agent policy unless the action has been explicitly pre-authorized by a human policy.

**Acceptance criteria**

- Absence of approval never implies permission to exceed budget or a mandatory boundary.
- Unrelated authorized work may continue while a mandatory action is held.
- Approval and denial are auditable.

### ACMS-REQ-027 - Bounded human intervention

**Sprint Priority:** 3

**Requirement**

ACMS shall minimize approval fatigue by batching deferable human decisions and allowing reversible in-scope work to proceed using documented fallback assumptions when approval is not mandatory.

**Acceptance criteria**

- Approval requests can define a timeout and default action.
- A timed-out reversible/deferable decision may preserve state, record an assumption, and continue authorized unrelated work.
- Mandatory human-gated actions default to not executing the gated action.
- The system can aggregate non-urgent items for scheduled co-work/review sessions.

### ACMS-REQ-028 - Human-attention and co-work queue

**Sprint Priority:** 3

**Requirement**

ACMS shall provide a human-attention queue for blocked work, required approvals, significant direction questions, and scheduled co-work sessions.

**Acceptance criteria**

- Each item identifies the work/agent involved, why human input is needed, available context, and proposed/default action.
- A human response can be recorded as overriding direction.
- Resolved attention items remain auditable.

### ACMS-REQ-029 - Direct human-agent interaction

**Sprint Priority:** 3

**Requirement**

ACMS shall allow an authorized human to send messages/steering to a registered agent and view the agent's responses in the context of the associated work while preserving the interaction in the audit trail.

**Acceptance criteria**

- Human messages are associated with agent/work/task context.
- Agent responses are distinguishable from ACMS control commands.
- The interface may use A2A messages for the underlying interaction.

### ACMS-REQ-030 - Authorized autonomy/sandbox policy

**Sprint Priority:** 3

**Requirement**

ACMS shall allow a human to define an autonomy policy for an agent or work scope that explicitly authorizes broad execution in a prototype/sandbox environment while still enforcing non-delegable budget, data-isolation, and environment boundaries.

**Acceptance criteria**

- High-autonomy policy is explicit and auditable.
- Sandbox autonomy does not implicitly authorize production, cross-tenant data access, or budget overruns.
- Policy can be revoked or narrowed by an authorized human.

---

## Feature Set: Agent Communication, Liveness, and Reliability

### ACMS-REQ-031 - A2A-based communication profile

**Sprint Priority:** 3

**Requirement**

ACMS shall use the Agent2Agent protocol as the primary semantic foundation for agent communication and shall define versioned ACMS extensions only where required for orchestration capabilities not represented by the base protocol.

**Acceptance criteria**

- The MVP interoperates through an A2A-compatible interface rather than an unrelated proprietary command model.
- ACMS-specific extensions are versioned and discoverable through capability metadata.
- Future A2A revisions can be adopted without changing ACMS agent identity or work-domain concepts.

### ACMS-REQ-032 - Simple MVP transport with real-time task streaming

**Sprint Priority:** 3

**Requirement**

The MVP shall implement the simplest practical A2A HTTP-based binding supported by the selected A2A implementation and shall support Server-Sent Events for live task/status updates where the agent advertises streaming capability.

**Acceptance criteria**

- ACMS can send a task/message without a persistent bidirectional socket requirement.
- Streaming-capable agents can emit live A2A task/status/artifact updates using SSE.
- Agents without streaming support can still be managed through supported non-streaming operations.

### ACMS-REQ-033 - Agent heartbeat/watchdog

**Sprint Priority:** 3

**Requirement**

Each registered agent/bridge shall provide a lightweight liveness heartbeat or equivalent health signal that ACMS can use to determine whether an expected agent is reachable and reporting.

**Acceptance criteria**

- ACMS records the latest heartbeat/health timestamp.
- Liveness can be distinguished from task progress.
- Heartbeat cadence and stale thresholds are configurable.

### ACMS-REQ-034 - Stale-agent reconciliation

**Sprint Priority:** 3

**Requirement**

ACMS shall actively reconcile an agent when expected heartbeats/status updates are missing and shall perform a periodic fleet-wide reconciliation independent of live streaming.

**Acceptance criteria**

- A working agent that misses the configured stale threshold can be actively queried.
- Failure to respond transitions the agent into a degraded/unreachable state rather than silently remaining healthy.
- A scheduled fleet-wide reconciliation can detect divergent ACMS/agent state even when no live failure was reported.

### ACMS-REQ-035 - Durable semantic events with sequence/correlation identity

**Sprint Priority:** 3

**Requirement**

ACMS shall durably retain significant semantic events with ordering/correlation information sufficient to reconstruct major work-state transitions without retaining every token-level streaming fragment.

**Acceptance criteria**

- Assignment, state changes, human/agent messages, decisions, approvals, errors, handoffs, artifacts, scope/budget events, and completion can be persisted.
- Events contain ordering or sequence/correlation identifiers sufficient for reconciliation.
- Ephemeral live stream fragments may be discarded after display/processing.

### ACMS-REQ-036 - Work item to execution-task traceability

**Sprint Priority:** 3

**Requirement**

ACMS shall allow one management work item to be associated with one or more A2A tasks/runs that collectively execute that work item.

**Acceptance criteria**

- A resumed/retried execution can create another A2A task without creating a new management work item.
- All associated execution tasks remain traceable to the same ACMS work item.
- A harness capable of maintaining one durable A2A task may do so without requiring artificial task splitting.

### ACMS-REQ-037 - Steering latency target

**Sprint Priority:** 3

**Requirement**

Under normal healthy network and agent conditions, ACMS shall deliver an accepted human steering instruction to a connected internal agent within 30 seconds.

**Acceptance criteria**

- An integration test can measure send-to-acceptance latency.
- Temporary network/agent outages are reported rather than counted as successful delivery.

---

## Feature Set: Security and External-Agent Isolation

### ACMS-REQ-038 - Private internal agent endpoints

**Sprint Priority:** 3

**Requirement**

Internal Agent Bridge endpoints shall be reachable only through approved private network/VPN paths and shall not be intentionally exposed directly to the public Internet in the MVP.

**Acceptance criteria**

- Deployment documentation defines the internal trust/network boundary.
- Public Internet access to internal bridge endpoints is not required for operation.
- Authentication and authorization are still required even on the private network.

### ACMS-REQ-039 - Authenticated and authorized agent control

**Sprint Priority:** 3

**Requirement**

ACMS shall authenticate communicating agents/bridges and shall authorize control operations against the agent identity, work scope, trust classification, and caller authority.

**Acceptance criteria**

- An unauthenticated caller cannot issue management commands.
- An authenticated but unauthorized caller receives a denial.
- Task/control access is scoped to the relevant agent/tenant/project boundaries.

### ACMS-REQ-040 - External Agent Gateway boundary

**Sprint Priority:** 3

**Requirement**

External/customer agents shall communicate with ACMS through a separately deployable External Agent Gateway that enforces Internet-facing authentication, tenant isolation, traffic policy, and protocol mediation instead of exposing the internal ACMS service directly.

**Acceptance criteria**

- External agents do not require direct network access to internal ACMS services.
- Gateway requests are associated with a specific external agent and tenant/customer scope.
- Internal and external agent traffic can be governed and audited separately.

---

## Feature Set: Cost, Metrics, and Agent Improvement

### ACMS-REQ-041 - Consume authoritative cost/usage metrics

**Sprint Priority:** 3

**Requirement**

ACMS shall consume cost, inference usage, model/runtime, and infrastructure metrics from authoritative external services such as LLM Manager and PDU Manager rather than independently calculating infrastructure cost.

**Acceptance criteria**

- Imported metric records identify source service and time interval.
- ACMS can associate received cost/usage data with agent/work/project dimensions when correlation data is provided.
- Disagreement with source-service cost is not silently corrected by ACMS.

### ACMS-REQ-042 - Cost attribution and projection dashboard

**Sprint Priority:** 3

**Requirement**

ACMS shall present current cost/usage, Gregorian calendar month-end projection, top spenders, and performance-to-cost information using authoritative imported metrics.

**Acceptance criteria**

- Authorized users can view current and projected cost.
- Top cost contributors can be grouped at least by agent and project/work scope when data is available.
- The dashboard distinguishes measured cost from projected/estimated cost.

### ACMS-REQ-043 - Budget policy enforcement

**Sprint Priority:** 3

**Requirement**

ACMS shall enforce human-approved spending/budget ceilings for agent work by preventing authorization of work that would knowingly exceed a hard approved limit and by requiring human approval to raise that limit.

**Acceptance criteria**

- A hard budget ceiling cannot be increased by an agent acting alone.
- Budget-limit events are auditable.
- ACMS can send budget/policy constraints to LLM Manager while allowing LLM Manager to choose how to satisfy the inference policy.

### ACMS-REQ-044 - Human feedback and performance history

**Sprint Priority:** 3

**Requirement**

ACMS shall preserve human feedback, peer-review outcomes, completion quality signals, scope adherence, and relevant execution outcomes as performance history for the responsible agent/version.

**Acceptance criteria**

- Feedback is traceable to the agent version and work item.
- Reviewer findings can affect the producer's performance history.
- Performance history does not silently rewrite a locked agent identity/version.

### ACMS-REQ-045 - Improvement proposals without silent identity mutation

**Sprint Priority:** 3

**Requirement**

ACMS may generate or receive recommendations to improve an agent's instructions, context, tools, or model selection, but shall not silently mutate a locked core agent identity/configuration or protected context.

**Acceptance criteria**

- Improvement proposals identify the evidence/reason for the recommendation.
- Changes to locked agent identity/configuration require authorized promotion to a new version or explicit unlock/change.
- Model/inference recommendations can be sent to LLM Manager/humans for implementation under approved policy.

---

## Feature Set: Dashboard and Human Situational Awareness

### ACMS-REQ-046 - Fleet/workforce status dashboard

**Sprint Priority:** 3

**Requirement**

ACMS shall provide a web dashboard that allows authorized users to understand what registered agents are doing, their primary assignments, status, recent significant activity, and whether human attention is required.

**Acceptance criteria**

- The dashboard shows each agent's identity, status, primary assignment, trust class, and last contact.
- The web interface is usable from a desktop browser and remains accessible on a mobile browser.
- The browser does not need direct connections to every agent; ACMS aggregates the view.

### ACMS-REQ-047 - Direction-to-progress visibility

**Sprint Priority:** 3

**Requirement**

ACMS shall show the most recent human-approved direction for a work scope and the reported progress toward completing that direction.

**Acceptance criteria**

- A user can identify the latest controlling direction for a project/feature/work item.
- Progress status and blockers are shown relative to that direction.
- Superseded direction is distinguishable from current direction.

### ACMS-REQ-048 - Goal/context-level progress metrics

**Sprint Priority:** 3

**Requirement**

ACMS shall support status/progress metrics at meaningful context levels so humans can evaluate whether product, project, feature, and task-level goals are advancing.

**Acceptance criteria**

- Metrics can be associated with multiple hierarchy levels.
- The UI distinguishes objective/measured metrics from agent-reported qualitative status.

### ACMS-REQ-049 - Kanban-style work view

**Sprint Priority:** 3

**Requirement**

ACMS shall provide a concise work-management view, including a Kanban-style representation or equivalent tagged-ticket view, for planned, active, blocked/review, and completed agent work.

**Acceptance criteria**

- Work items can be filtered by project, agent/manager, status, and priority where available.
- Human-attention items are visually distinguishable from ordinary active work.

---

## Feature Set: Scalability and Resilience

### ACMS-REQ-050 - Avoid per-action centralized execution dependency

**Sprint Priority:** 3

**Requirement**

ACMS architecture shall not require every agent tool invocation or internal execution step to traverse ACMS, so normal agent execution can continue without making ACMS the throughput bottleneck.

**Acceptance criteria**

- Agents execute approved local steps directly inside their authorized environment.
- ACMS receives significant state/events and exercises governance/control without proxying every low-level action.
- Loss of the live dashboard stream does not automatically terminate safe local execution already authorized.

### ACMS-REQ-051 - Initial fleet scalability target

**Sprint Priority:** 3

**Requirement**

The initial ACMS architecture shall support at least 100 concurrently active registered agents on the intended MARION-IA-USA deployment without requiring one browser connection per agent or synchronous polling of every agent for routine status.

**Acceptance criteria**

- Routine status is event/heartbeat driven when available.
- Browser clients consume aggregated ACMS state/events rather than connecting directly to each agent.
- Load testing can demonstrate the target before production acceptance.

