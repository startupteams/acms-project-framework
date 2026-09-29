# Speed-to-Lead / External-Agent Architecture — Decisions & Boundary Package (Phase F, 2026-09-29)

**Recorded human decisions (2026-09-29):**

1. **Speed-to-Lead = a separate, evolving product/repository** — not an ACMS module.
2. **ACMS = internal orchestration infrastructure** (Startup Teams' own AI workforce control plane).
3. **External/customer Hermes agents start sanitized and blank** — fresh profile, no memories, no
   internal skills, no company instructions, no secrets.
4. **Internal company state must never be inherited** by an external agent (hard boundary).
5. **Speed-to-Lead is the first external agent class** (real-estate lead-response agent product).
6. **First portal persona = real-estate broker owner/operator.**

These six decisions are treated as **accepted product direction** (human-stated). Everything below
is architecture derived from them; open product questions stay with the human (§4).

## 1. Repository strategy

**Proposal: `agentifyme-external-agent-base`** (separate repo, under jordatech/ or startupteams/
per Jordan's choice — repo creation NOT executed; needs authorization per the startupteams rule).

**Safe to reuse from `hermes-base-setup`** (generic mechanisms only):
- `.gitignore` strategy (profiles exclusions, secret-file patterns);
- secret-scan approach (pre-commit + push-protection backstop; the 2026-09-24 lesson list);
- bootstrap/version metadata shape (`build_info`-style identity);
- state-sync mechanics (mirror/backup + health reporting) as a **pattern**, pointed at a new repo.

**Must NOT copy:**
- Startup Teams internal memories/MEMORY.md content, SOUL internal-preamble, internal skills,
  internal company instructions, internal secrets/configuration, internal hostnames/topology.

**Baseline contents:** blank external Hermes profile, external trust-class manifest, tool
allowlist placeholder, per-tenant config slots, state-sync pointed at the product's own repo,
sanity test that asserts NO internal markers (memories/skills/hosts) are present.

## 2. Gateway boundary — a service gateway is more than a firewall

See `EXTERNAL_AGENT_PRODUCT_BOUNDARY_OPTIONS.md` for the full comparison. The key point: the
boundary must place **authentication, tenant identity/isolation, rate/spend limits, payload
validation, tool allowlists, guardrails, auditing, and A2A adaptation** somewhere deliberate —
"gateway" is a role, not a box.

## 3. Threat model (highest-risk assumption)

The human-identified top risk: **"the external boundary actually isolates customers and enforces
adequate guardrails."** Evidence-oriented test plan lives in the boundary doc (§4 there).

## 4. Open product decisions (remain with the human — do NOT invent)

- Portal UX specifics (Phase G discovery feeds this; no redesign until human reviews).
- Commercial model (per-lead? per-seat? outcome-priced?) — noted as the Speed-to-Lead product
  question; ACMS work-management compatibility is independent of it.
- Multi-tenant data residency & retention windows for customer lead data.
- Which LLM providers are customer-visible (LLM Manager is internal; external agents likely route
  via cloud APIs with per-tenant keys + caps — mirrors the LLM Manager §9.3 caps-first rule).
