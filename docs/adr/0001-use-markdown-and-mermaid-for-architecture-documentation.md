# ADR-0001: Use Markdown and Mermaid for architecture documentation

**Status:** Accepted  
**Date:** 2026-09-25

## Context

ACMS is designed for humans and AI agents to collaborate rapidly. Architecture needs to remain diffable, readable, version-controlled, and easy for agents to update without a separate documentation toolchain.

## Decision

Use Markdown as the primary documentation format and Mermaid as the default diagram format. Keep a single `docs/ARCHITECTURE.md` as the main architecture document and use ADRs for durable decisions.

## Consequences

Documentation stays human-readable and Git-friendly. Advanced architecture modeling tools may be introduced later only if a real need appears.

## References

- README.md
- docs/ARCHITECTURE.md
