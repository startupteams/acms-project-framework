# AGENTS.md

Instructions for autonomous and semi-autonomous agents working in ACMS.

## Authority model

Humans set product intent, approve scope, approve significant decisions, define mandatory governance boundaries, and approve merges.

Agents may inspect the repository, plan, implement approved requirements, decompose approved scope into tasks, create tests, update affected documentation, create handoffs, add out-of-scope discoveries to `FUTURE_WORK.md`, draft ADRs/TDRs, and execute non-destructive work inside explicitly approved scope/budget/security/environment boundaries.

Agents must not silently add/remove product scope, promote future work, accept their own new significant ADR, merge their own feature work, exceed a budget, cross tenant/context boundaries, deploy to production, alter credentials/permissions, perform destructive infrastructure actions, or modify protected Organization/Product/Project/Feature context without required human authorization.

## Read before planning

For every meaningful change read:

1. `README.md`
2. `REQUIREMENTS.md`
3. `SPRINT.md`
4. `docs/BUSINESS_PROBLEMS.md`
5. `docs/ARCHITECTURE.md`
6. relevant accepted/proposed ADRs
7. `FUTURE_WORK.md` when scope/follow-up work is involved
8. `INITIAL_IDEAS.md` when source provenance matters

## Requirement discipline

Implementation must trace to one or more `ACMS-REQ-###` entries. Before editing summarize the requirement IDs, acceptance criteria, files/components likely to change, assumptions, and validation.

If work is outside approved requirements, ask for human approval or record it in `FUTURE_WORK.md`.

## Future-work provenance

Every future-work item must identify one of:

- **Human-Directed**
- **Agent-Discovered**
- **Source-Derived**

Human-Directed does not mean approved; it remains future scope until promoted.

## Centralized governance, decentralized execution

ACMS controls approved assignment, scope, budget, context access, permissions, and governance. It must not become the synchronous permission service for every shell command/tool call/test/retry.

A worker agent may autonomously execute implementation details inside its approved envelope.

An agent may have:

- one primary ACMS work assignment; and
- zero or more registered background routines/cron jobs.

## Context authority

Protected context levels:

- Organization
- Product
- Project
- Feature

Agents may propose changes but may not modify these without human authorization or explicit delegated authority.

Agent, Task, and Live Session context may be modified within approved policy.

If approved scope appears wrong, execute it as far as reasonably possible, preserve state, create a handoff, and propose the alternate direction instead of silently changing scope.

## Handoffs

Meaningful work must produce/update a Markdown handoff recording:

1. assigned goal and requirement IDs;
2. what was attempted;
3. what actually changed;
4. what the agent believes the current state is;
5. validation and results;
6. blockers/assumptions/unresolved questions;
7. recommended next action.

## ADR policy

Human-explicit durable architecture decisions may be recorded as `Accepted`. Agent-originated significant decisions remain `Proposed` until human approval.

## Approval timeout policy

- Reversible in-scope decision: checkpoint/preserve state, use documented fallback, continue if safe.
- Consequential but deferable action: defer that action and continue unrelated work.
- Mandatory human gate: never infer approval from timeout; hold/deny the gated action and continue only unrelated authorized work.

## Security

- Never commit credentials/secrets/customer-sensitive data.
- External/customer agents are a separate trust domain.
- Internal Agent Bridge endpoints are private-network/VPN endpoints unless an accepted ADR says otherwise.
- Do not weaken authentication/authorization/audit to simplify implementation.

## Git workflow

The documentation bootstrap is human-authorized for direct import. After bootstrap, feature work must use a dedicated branch and PR.

Recommended branch: `type/ACMS-short-description`.

Recommended commit: `type(scope): short description (ACMS-REQ-###)` plus issue/ticket when available.

Every feature PR must list requirement IDs, acceptance criteria, validation, risks, ADR/TDR changes, future work, and reviewer focus. Agents do not self-merge.
