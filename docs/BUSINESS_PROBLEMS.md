# ACMS Business Problems and Product Intent

## Purpose

AgentifyMe Cloud Management System (ACMS) is an internal software-management and AI-workforce orchestration product for Startup Teams / AgentifyMe. Its first deployment is intended to coordinate software-development agents operating primarily on the MARION-IA-USA infrastructure with locally hosted models delivered through LLM Manager.

ACMS is infrastructure for building and operating AgentifyMe's agentic products, including the first Speed-to-Lead product, but ACMS is not limited to that product.

## Primary business problems

### 1. AI-agent context is fragmented and easy to lose

Agent work can span many sessions, models, harnesses, restarts, and humans. Without durable structured context, an agent may forget decisions, retrieve obsolete direction, repeat work, or diverge from approved scope.

**Desired outcome:** ACMS provides durable, versioned, status-aware context from Organization through Live Session while protecting higher-level context from unauthorized changes.

### 2. Agents change over time but successful behavior must remain repeatable

Prompts, instructions, models, tools, and contextual knowledge can change an agent's behavior. Continuous uncontrolled change makes it difficult to reproduce a working pipeline or understand why performance improved or degraded.

**Desired outcome:** persistent agent identities, versioned/lockable agent configurations, performance history, human feedback, peer review, and controlled improvement proposals.

### 3. AI-created work needs planning, ownership, traceability, and completion criteria

A collection of chat sessions does not answer which projects are moving, what was assigned, what is blocked, which requirement a task supports, or whether a goal is complete.

**Desired outcome:** durable product/project/feature/work-item hierarchy, one primary assignment per worker, manager-led decomposition inside approved scope, Kanban-style visibility, artifacts, and handoffs.

### 4. Humans need control without becoming the throughput bottleneck

If every agent action requires approval, humans will be overloaded and eventually bypass governance. If agents never require approval, they can exceed scope, budget, security boundaries, or production safety limits.

**Desired outcome:** bounded human intervention. Humans approve consequential boundaries while agents autonomously execute reversible work inside approved scope. Questions are batched into human-attention/co-work sessions where practical.

### 5. Agent activity and cost need to be understandable in the same operational picture

Locally hosted models consume electricity and compute; cloud models and tools may create direct spend. Raw infrastructure measurements alone do not explain which project, task, or agent created the cost.

**Desired outcome:** LLM Manager/PDU Manager remain authoritative for raw metrics; ACMS correlates those metrics to agents/work and shows current spend, month-end projection, top spenders, and performance-to-cost relationships.

### 6. Human operators need immediate situational awareness

A human should be able to answer quickly:

- What did I most recently direct?
- How close is that direction to completion?
- What is every agent doing?
- Which projects/features are advancing or stalled?
- What needs my attention?
- What failed or changed direction?
- What are we spending now and likely to spend this month?

**Desired outcome:** a single web dashboard aggregating workforce, work, attention, audit, context, and metrics.

### 7. Different agents and harnesses need one management vocabulary

Hermes, Pi, Codex, and future harnesses may expose different controls and runtime behavior. Tying ACMS directly to one harness would make the product brittle.

**Desired outcome:** standardized ACMS Agent Bridge + A2A-based capability communication. Harness differences remain behind adapters.

### 8. External/customer agents create a different security problem

ACMS may soon coordinate dedicated agents supporting AgentifyMe customers. Client-sensitive information must not mix with internal organizational context or another customer's information.

**Desired outcome:** explicit Internal/External trust classification, tenant isolation, protected context, and a separately deployable External Agent Gateway for Internet-facing customer-agent traffic.

## Product boundaries

### ACMS owns

- agent registry and persistent identity;
- work/project orchestration;
- human steering and approvals;
- context governance and visibility;
- audit history;
- performance/review history;
- background-routine inventory;
- significant-event ingestion;
- cost/usage attribution and presentation;
- dashboard and human-attention workflows;
- policy envelopes sent to infrastructure dependencies.

### ACMS does not own

- provisioning agent VMs/containers;
- installing/instantiating harnesses;
- starting model servers;
- selecting infrastructure placement for model servers;
- calculating authoritative electrical cost;
- directly managing vLLM/model-serving infrastructure.

These belong primarily to LLM Manager, PDU Manager, and infrastructure tooling.

## Product principles

1. **Centralized governance, decentralized execution.**
2. **Human intent outranks agent-discovered convenience.**
3. **Approved scope is executed, not silently reinterpreted.**
4. **Important semantics are durable; incidental token noise is not.**
5. **Repeatability requires versioning and locking.**
6. **Context access follows least privilege, especially across customer boundaries.**
7. **ACMS should remain useful even if individual agents/harnesses/models change.**
8. **ACMS is a control plane, not a replacement for every agent-local service.**
