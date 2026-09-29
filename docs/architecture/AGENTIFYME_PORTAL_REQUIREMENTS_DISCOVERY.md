# AgentifyMe Portal Requirements/Mockup Discovery (Phase G, 2026-09-29)

**Method:** read-only discovery across Startup Teams repos and local files. **No UI redesign; no
frontend implementation** until the human reviews this and confirms the portal architecture.

## Artifacts found (all real, verified read-only)

| Artifact | Location | What it is |
|---|---|---|
| **Speed-to-Lead frontend prototype** | `startupteams/agentifyme-speed-to-lead` (private; default branch `AGENT_STEA003_AI_ML_SOFTWARE_ENGINEER`, last push 2026-09-28) | React+TS/Vite clickable prototype: LeadsDashboard, ConversationDetail, ConversationsInbox, Sidebar/Header; `LeadAdapter` interface with mock data (synthetic leads only); Vitest tests |
| Reference design audit | `AUDIT-MAP.md` (same repo) | 3 reference screenshots described in text: dashboard (metric cards 24 Total / 22 AI Responded / 18 Qualified / 5 Human Review / 7 Showings; lead table w/ source badges, intent, priority, assigned-to), lead-detail conversation view (AI bubbles, Extracted Information, Lead Timeline, AI summary, Quick Actions Take Over/Mark Handled/Reassign/Pause AI/Archive), out-of-scope view (analysis panel, handoff status, broker notification) |
| P0 checklist | `docs/P0-REQUIREMENTS-CHECKLIST.md` | 15/15 prototype requirements traced ✅ (week-2 of 3-week sprint) |
| UX questions | `docs/PROTOTYPE-UX-QUESTIONS.md` | 10 open questions (state machine rules, speed-metric definition, contact-method chooser, failed/unavailable boundary, mobile action bar, retry behavior…) |
| Demo coordination | `docs/HIGH-POINT-DEMO-ADAPTATION-NOTES.md` + `COORDINATION-NOTES-ROOT.md` | High Point Realty & Auction / Ken DeGrant pilot context; Utkarsh owns confirmations (lead sources, property types, SLAs, branding) |
| Pricing/positioning | `~/business_idea_generator/data/pricing_strategy_agentifyme.md` | Tiers (Free "Starter Agent" 1 lead / Go $19 50 leads / Pro $49-79), **"The Hook is Speed"** — every lead answered <60s, 24/7; pain→metric mapping (lead response time, hours saved, follow-up touchpoints, CRM auto-logs) |
| Product concept | `~/business_idea_generator/data/ideas/agentifyme-ai-as-a-service.md` | AgentifyMe AI-as-a-Service; beachhead personas: (1) Midwest real-estate brokers scaling 5-30→50+ agents, (2) time-poor entrepreneurs |
| Older infra repos | `~/agentifyme_repo`, `agentifyme_server_setup` (local) | infrastructure/hermes setup notes — NOT portal |

## Personas (current, evidence-based)

1. **Real-estate broker owner/operator (FIRST portal persona — human-confirmed 2026-09-29).**
   Concrete instance in the artifacts: Ken DeGrant, High Point Realty & Auction. Broker-facing
   screens: dashboard metric cards, leads table, conversation takeover, out-of-scope handoff review.
2. Real-estate AGENTS (recruited by brokers — secondary, implied by the recruiting use case).
3. Entrepreneurs (beachhead 2, deferred in the artifacts).

## Current screens (from prototype + audit map)

1. **Dashboard** — 5 metric cards (total/AI-responded/qualified/human-review/showings), activity
   chart, integrations panel (SMS/Email/Web/Form/Zillow/Realtor.com), knowledge-base card.
2. **Leads table** — source badge / intent tag / location / price / status / priority / assigned-to.
3. **Lead detail / conversation** — AI + human message thread, extracted info sidebar
   (intent/location/max price/bedrooms/garage/preference/property type), timeline, AI summary with
   recommended steps, quick actions (Take Over, Mark as Handled, Reassign, Pause AI, Archive).
4. **Out-of-scope review** — flagged conversation, detected issue analysis, handoff status
   (reassigned to human, awaiting broker acknowledgment), broker notification, recommended actions.
5. Nav: Dashboard / Leads / Conversations / Calendar / Knowledge Base / Brokers & Teams / Settings.

## Top actions implied by the screens

- Respond/take over a conversation (human takeover mid-thread).
- Reassign lead to a human; pause AI; archive; mark handled.
- Configure AI behavior (knowledge base, voice/tone — per pricing doc).
- Monitor speed metric (time-to-first-contact/response) — **definition unresolved** (UX Q2).
- Manage team (brokers & teams), integrations (CRM, lead sources), billing/tiers.

## Data the backend must supply (implied contract)

- Leads: id, source, intent, location, price range, property prefs, status (7-state machine),
  priority, assigned-to, timestamps for timeline + speed metrics.
- Conversations: messages (role=ai/human/customer), extracted-info record, AI summary, out-of-scope
  flag + analysis, handoff status + broker acknowledgment.
- Org/tenants: broker org, team members (assigned agents), knowledge-base docs, integrations, tier.
- Speed metrics: first-contact/response latency per lead + aggregate cards.
- Out-of-scope analysis records (detected issue, why flagged, recommended actions).

## Backend assumptions (current direction, not yet built)

- Speed-to-Lead = separate product repo (human decision 2026-09-29); external agent class #1;
  agents start sanitized/blank (no internal state) — see
  `SPEED_TO_LEAD_EXTERNAL_BOUNDARY_DECISIONS.md` + `EXTERNAL_AGENT_PRODUCT_BOUNDARY_OPTIONS.md`.
- The prototype's `LeadAdapter` interface is the natural seam for the future real backend.
- External agent (lead-response) runs per tenant behind the External Agent Gateway; the portal is
  the human control surface (take over, pause AI, reassign).

## Contradictions / missing decisions (for the human)

1. **Speed metric undefined** (time-to-first-contact vs first-response; UX Q2) — matters because it
   is THE hook metric on the pricing page (<60s).
2. **Lead state machine rules** (failed↔contacted transitions; failed vs unavailable boundary; UX
   Q1/Q5) — no documented criteria.
3. **`empty` state ambiguity** (no-notes vs no-leads; UX Q3).
4. **Contact-method chooser** (adapter supports method; UI doesn't expose it; UX Q4) — and SMS/phone
   reality requires provider choices (Twilio etc.) — none selected yet.
5. **High Point demo inputs pending from Utkarsh** (lead sources, property types, SLAs, branding) —
   the demo path is blocked on a human outside this agent's reach.
6. **Multi-tenant portal vs single-org pilot**: prototype is single-org (org selector in header
   per audit map, but mock data is one org); production tenancy shape is undecided (ties to F).
7. **Pricing tiers vs gateway spend caps**: pricing doc tiers (1/50/unlimited leads) must map to
   per-tenant LLM spend caps in the gateway — mapping not yet defined.

## Unanswered product questions (do NOT invent)

- Exact launch scope for the first paying pilot (High Point demo → which screens are contract-min?).
- Billing provider + trial mechanics.
- Where the knowledge base lives and who curates it.
- SMS/phone/email provider accounts + compliance (TCPA/A2P 10DLC for US texting) — real-world
  prerequisite for the core loop, owned by the human.
