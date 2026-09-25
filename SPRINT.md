# Current Sprint - ACMS Foundation

## Sprint Goal

Establish the first executable ACMS control-plane scaffold and prove the registry/Agent Bridge/A2A foundation without expanding into dashboard, external gateway, or advanced orchestration work.

## Features in Scope

### Control-plane scaffold

**Requirements**

- `ACMS-REQ-001`
- `ACMS-REQ-003`
- `ACMS-REQ-004`
- `ACMS-REQ-031`
- `ACMS-REQ-038`
- `ACMS-REQ-039`

**Work items**

- [ ] Create Proposed ADR for MVP implementation stack and authentication mechanism.
- [ ] Create backend service skeleton.
- [ ] Add health/readiness/version endpoints.
- [ ] Define initial persistent agent registry model.
- [ ] Define A2A Agent Card/capability adapter model.
- [ ] Add minimal authenticated registration/control boundary.
- [ ] Add deterministic tests and documented setup/run/test commands.
- [ ] Create sprint handoff.
- [ ] Open PR for human verification; do not merge autonomously.

## Explicitly out of scope for this sprint

- Full dashboard UI.
- External Agent Gateway implementation.
- Semantic/vector context retrieval.
- Cost integration.
- Production deployment.
- Agent instantiation.
- Managing vLLM/model servers.
- gRPC or multiple A2A transport bindings.
- Internal event bus.

## Definition of Done

- [ ] Proposed stack/auth ADR is visible to human reviewer.
- [ ] In-scope acceptance criteria are satisfied or explicitly deferred by human decision.
- [ ] Tests/checks pass.
- [ ] README contains exact local setup/run/test commands.
- [ ] No secrets are committed.
- [ ] Documentation affected by the scaffold is updated.
- [ ] Pull request clearly states validation, risks, and reviewer focus.
- [ ] Human reviewer approves before merge.

## Risks / Blockers / Human Decisions Needed

- Human must approve the concrete MVP implementation stack and authentication mechanism in the first feature PR before merge.

## Sprint Handoff

At sprint completion, record shipped requirements, validation results, accepted/rejected ADR choices, unresolved work, and recommended next PR.
