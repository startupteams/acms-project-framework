# Direction to LLM Manager from ACMS Discovery

**Date:** 2026-09-25  
**Authority:** Human product direction captured during ACMS design discovery

## Purpose

This document defines responsibilities delegated to LLM Manager so ACMS can remain an orchestration/control plane rather than absorbing inference infrastructure and agent-instantiation responsibilities.

## LLM Manager owns

1. **Agent instantiation/bootstrap**
   - Create/configure the underlying agent execution environment through approved processes.
   - Install/configure the selected harness where applicable.
   - Bootstrap the ACMS Agent Bridge or provide the information needed for the agent/creator to register with ACMS.
   - Support human/manual modifications and future automated provisioning workflows.

2. **Model and inference infrastructure**
   - Manage local model-serving endpoints and the infrastructure required to run them.
   - Track model/server availability, throughput, utilization, and provider configuration.
   - Decide how to satisfy human-approved ACMS inference constraints/policies.
   - ACMS may recommend a model/inference change, but LLM Manager/humans remain the implementation authority for persistent inference/model policy changes.

3. **Authoritative inference usage and cost data**
   - Calculate/collect inference usage and model/runtime cost metrics.
   - Combine infrastructure inputs as appropriate with authoritative PDU/power measurements rather than requiring ACMS to recreate those calculations.
   - Expose metrics to ACMS through a stable machine-readable interface.

4. **Agent registration support**
   - Provide ACMS with enough bootstrap information to associate the instantiated agent with its persistent ACMS identity.
   - Coordinate the initial endpoint/Agent Card/capability registration through the Agent Bridge.
   - Preserve the separation between agent identity in ACMS and runtime process/session identity.

## ACMS owns

- projects/features/work items;
- primary assignment and reassignment;
- agent context governance;
- human steering and approvals;
- audit history;
- performance/peer-review history;
- budgets/policy envelopes at the work/agent/project level;
- human-attention/co-work workflow;
- presentation and attribution of imported cost/usage metrics.

## Required LLM Manager -> ACMS data contract (initial direction)

The future API should be able to provide, where available:

- persistent ACMS agent ID or registration correlation ID;
- execution host/location;
- harness/runtime type and version;
- active model and inference endpoint identity;
- local/cloud provider classification;
- usage counters appropriate to the model/runtime;
- measured/derived inference cost for a requested interval;
- server/GPU availability and health summaries useful to policy decisions;
- timestamps and correlation IDs allowing cost/usage to be attributed to ACMS work/run/agent;
- model capability/benchmark metadata useful for reviewer/model-selection recommendations when maintained by LLM Manager.

## ACMS -> LLM Manager policy/request contract (initial direction)

ACMS should eventually be able to send approved constraints/recommendations such as:

- preferred/local-only/local-first inference intent;
- hard spending ceiling applicable to an agent/work scope;
- allowed/disallowed provider/model classes;
- recommendation to change a model or inference configuration;
- reviewer-selection request or model-capability query;
- agent registration/bootstrap metadata.

A model/inference recommendation does **not** itself authorize LLM Manager to violate human-approved policies or budgets.

## Explicit non-goals

- ACMS will not start/stop vLLM servers directly.
- ACMS will not independently calculate authoritative electricity cost.
- ACMS will not silently rewrite persistent model/inference policy.
- LLM Manager will not become the project/work-management system.

## Follow-up implementation work for LLM Manager

1. Define a versioned ACMS integration API.
2. Define correlation IDs between inference requests/cost records and ACMS agent/work/run IDs.
3. Define registration/bootstrap exchange for Agent Bridge deployment.
4. Expose model capability/benchmark information if available for peer-review/reviewer selection.
5. Document failure modes when metrics or model infrastructure are unavailable.
