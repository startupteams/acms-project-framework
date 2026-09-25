# ADR-0005: Separate internal and external/customer agent trust paths

**Status:** Accepted  
**Date:** 2026-09-25

## Context

ACMS initially manages internal software-development agents but is expected to coordinate customer-dedicated agents. Customer-sensitive data and public network exposure require stronger segregation.

## Decision

Classify agents as Internal or External. Internal Agent Bridges use approved private network/VPN paths. External/customer agents communicate through a separately deployable External Agent Gateway that provides Internet-facing authentication, tenant isolation, and protocol mediation; they do not connect directly to internal ACMS services.

## Consequences

External connectivity is isolated from internal traffic and context. A separate gateway component must eventually be built and governed.

## References

- ACMS-REQ-022
- ACMS-REQ-024
- ACMS-REQ-038
- ACMS-REQ-040
