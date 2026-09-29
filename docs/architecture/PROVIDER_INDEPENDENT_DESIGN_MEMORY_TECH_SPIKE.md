# Provider-Independent Design-Memory Tech Spike (Phase H, 2026-09-29)

**Human direction:** do not rely permanently on ChatGPT as the only durable design-memory provider.
**Status:** analysis + MVP migration path. No vector DB was built (correctly, per direction).

## What "design memory" means here

The durable, reusable reasoning the team produces: venture breakdowns, DE-canvas analyses, business
idea reviews, architecture decisions (ADRs/TDRs), market specs, validated lessons. Today much of it
lives in ChatGPT conversations + scattered `.md` files + repo docs — provider-captured.

## Options compared

| Criterion | A. ACMS relational + Markdown artifacts | B. Object store + metadata DB | C. Git/Markdown | D. Document DB | E. Vector index layered on canonical store | F. Dedicated Design-Memory service | G. Hybrid |
|---|---|---|---|---|---|---|---|
| Human readability | high (md in UI/repo) | medium | **high** | medium | n/a (index only) | medium | high |
| Provider independence | **high** | high | **high** | high | high (if index rebuildable) | high | high |
| Search | SQL + grep | metadata only | grep/code-search | good | **best (semantic)** | good | best |
| Versioning | created_at rows | object versions | **git history** | app-level | index rebuild | app-level | git + objects |
| Provenance | fields | fields | commit metadata | fields | references canonical | fields | fields |
| Retention/cost | cheap | cheap | cheap | moderate | index storage | service cost | moderate |
| Backup/restore | PG dump | bucket sync | git clone | DB dump | **rebuildable** | DB dump | layered |
| Tenant isolation | row-level (later) | prefixes | repo perms | app-level | per-tenant index | app-level | layered |
| Reconstruct context for another model | prompt from md | fetch + prompt | fetch + prompt | fetch + prompt | retrieval + prompt | API | retrieval + prompt |

**Key insight:** vector search is a **retrieval/indexing layer** over canonical storage — never the
canonical store itself. An index must always be rebuildable from the canonical store (else the
provider owns you again).

## Recommendation — Hybrid (C as spine + A where structure helps + E later)

**Canonical store = Git/Markdown** (provider-independent by construction, diffable, reviewable,
backed up to the local mirror per the Agent-State spike D3):

1. **Where design memory already lands, keep it landing there:** ADRs/TDRs, architecture spikes,
   handoffs (this session's Phase B–H docs are exactly this), business-idea `.md` files
   (business_idea_generator already uses a git-tracked data/ dir).
2. **Add a light index where structure pays:** ACMS already has durable relational rows for
   work/economics; a small `design_memory` metadata table (id, path, title, type, tags, created_by,
   source_refs) gives queryable provenance WITHOUT moving the content. Optional; only if search
   across many docs becomes a daily need.
3. **Vector index = later, optional, rebuildable:** only when corpus > ~hundreds of docs and
   semantic recall demonstrably beats keyword search. Index points at git paths; rebuild-from-git is
   the acceptance test (delete the index, rebuild, lose nothing).

## MVP migration path from the ChatGPT-heavy workflow

1. **Export habit (no new system):** after every meaningful ChatGPT design session, the conclusions
   land as a Markdown file in the owning repo (business_idea_generator/data, acms docs/, or a new
   `design-memory/` dir in the relevant repo) with a provenance header (source: ChatGPT chat, date,
   prompt topic). A simple `chat_export_checklist` in the team SOP.
2. **Bulk import once:** existing valuable ChatGPT threads → hand-curated `.md` files (human triage;
   no blind dump — the 2026-09-24 lesson: exports carry secrets; run the secret scan).
3. **Index later:** the metadata table / vector layer only when volume justifies.

## Explicitly rejected for now

- Dedicated Design-Memory service: duplicates git + ACMS + handoff conventions for no current need.
- Making vector DB canonical: violates rebuildability; creates a new single point of capture.
- Document DB: no advantage over md+git at current scale.
