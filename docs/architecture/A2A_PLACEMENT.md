# A2A Placement (Phase F5, 2026-09-29)

**Question:** where does the A2A protocol boundary belong?

```text
(a) ACMS ↔ Bridge/Agent          — EXISTS TODAY (proven live 2026-09-29, Phase A)
(b) Gateway ↔ Bridge/Agent       — the External Agent Gateway invoking agent runtimes
(c) Product Backend ↔ Gateway    — product-to-gateway transport
```

## Analysis

- **(a) stays A2A:** internal ACMS ↔ Hermes api-server is the proven, tested contract
  (HARNESS_CONTROL_MAPPING; status/send_work/steer/interrupt/cancel verified live). Keep.
- **(c) does NOT need A2A:** product→gateway is an internal service call; plain authenticated JSON
  APIs are simpler, typed, and easier to version than a task protocol. A2A here would add a protocol
  where a function call suffices.
- **(b) is the real decision.** The External Agent Gateway must start agent runs, stream/collect
  results, steer/interrupt — exactly the A2A verb set. Two sub-options:
  - **B1 — Gateway speaks A2A to runtimes** (reuses the Hermes adapter contract directly).
  - **B2 — Gateway exposes its own Run API; A2A stays an internal adapter detail** behind it.

## Recommendation

**Prefer standards-compatible boundaries, but do not force A2A at every layer:**

- Keep **(a) A2A** (internal, proven).
- Design **(b) as A2A-shaped**: the gateway's outward contract (start/steer/interrupt/status)
  mirrors the A2A verb set so B1 is the default implementation, but the gateway MAY adapt to
  non-A2A runtimes behind the same contract (harness-neutral management is the stated product
  goal — Hermes, Pi, Ruflo, OpenClaw).
- Keep **(c) non-A2A** (plain internal API).

This gives protocol reuse where it pays (runtime control) without coupling the product to a
task-protocol hop where it doesn't.
