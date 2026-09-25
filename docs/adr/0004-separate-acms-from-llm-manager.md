# ADR-0004: Separate ACMS orchestration from LLM Manager instantiation and inference ownership

**Status:** Accepted  
**Date:** 2026-09-25

## Context

Agent instantiation, model serving, inference selection, and infrastructure cost measurement are distinct responsibilities from project/work orchestration.

## Decision

LLM Manager and approved provisioning workflows instantiate/register agents and manage model/inference infrastructure. ACMS orchestrates registered agents, provides work/policy constraints, consumes authoritative usage/cost metrics, and may recommend model/inference changes without directly managing model servers.

## Consequences

Both systems retain clear responsibilities. Integration APIs and correlation IDs are required between them.

## References

- ACMS-REQ-002
- ACMS-REQ-041
- ACMS-REQ-043
- docs/LLM_MANAGER_DIRECTION.md
