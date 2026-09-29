# Jira ↔ ACMS Work-Management Integration Options (Phase E, 2026-09-29)

**Context:** the human already uses Jira; ACMS must be compatible with Jira-like work management.
**Status:** options analysis + recommendation. **No connector implemented; no Jira mutation.**

## The authority question (decide BEFORE building a connector)

The single most consequential decision is **who owns status truth**. Everything else (ID
correlation, status mapping, failure handling) follows from it.

## Options

### A. Jira authoritative for human planning; ACMS manages AI execution beneath Jira issues
- Jira issue = the human-facing unit (backlog, sprints, board). ACMS Work Items execute under each
  issue; ACMS never writes status upward except via comments/remote-links.
- Pros: humans keep their existing tool and mental model; ACMS scope stays "AI execution layer";
  no two-way sync loops. Jira's workflow lock/power-ups stay intact.
- Cons: two IDs (JIRA-123 ↔ ACMS-WORK-…); ACMS dashboards must join across systems; AI progress
  visibility in Jira is limited to what the sync pushes (comments/links).
- Fit: strong for companies whose humans already live in Jira (**the human's situation**).

### B. ACMS authoritative; selected Work/Feature status mirrored to Jira
- ACMS owns the machine-plane truth; a one-way mirror projects selected state into Jira.
- Pros: single source of truth for AI work; simplest correct model; no conflict resolution.
- Cons: Jira users see a projection, not the system of record; human planning (backlog grooming)
  happens outside ACMS, weakening Jira's role to read-only.

### C. Bidirectional synchronization
- Both systems write; a sync engine resolves conflicts.
- Pros: flexible.
- Cons: **status ping-pong, conflict rules, retry semantics, rate limits** — the classic
  two-masters trap; Jira webhooks + ACMS events need idempotent, ordered, retryable sync with
  drift repair. Highest complexity, highest silent-drift risk. Reject for v1.

### D. Jira parent issues; ACMS-only AI micro-work; summary/progress sync upward
- Jira issue = parent (maps to ACMS Feature/Program); each measurable goal = ACMS Work Item
  (Phase B lifecycle); ACMS posts periodic summary comments + a remote link per child; completion
  posts one final handoff comment.
- Pros: matches the measurable-goal lifecycle exactly; Jira stays the human planning surface;
  sync is **one-way and append-only (comments + links)** → no status conflicts at all; failure
  handling trivial (retry a comment).
- Cons: Jira issue status itself isn't auto-moved unless the human chooses A-style upward writes.

**Linear comparison (only where it materially differs):** Linear has a cleaner API (GraphQL,
first-class external-ID fields, webhooks) and cheaper rate limits than Jira Cloud; its "Triage
views + auto-labeling" fit AI-proposed work well. But the human's tooling is Jira — Linear is
relevant only as a future alternative with the **same Option-A/D shape** (API differences don't
change the authority model). ACMS should keep its Jira adapter behind the same interface so a
Linear adapter is a drop-in later.

## Evaluation matrix

| Criterion | A | B | C | D |
|---|---|---|---|---|
| Source-of-truth conflicts | none (Jira plans, ACMS executes) | none (one-way out) | many | none (one-way comments) |
| ID correlation | JIRA-123 ↔ work_key/metadata | same | same + sync ids | parent-link table |
| Status mapping | none needed | simple out-map | two-way + rules | none needed |
| Offline/failure handling | queue comments | queue mirror | **hard** (retry+repair) | queue comments |
| Comments/handoffs | in both | Jira read-only | both | Jira gets summaries |
| Permissions | Jira own / ACMS own | ACMS own | mixed | Jira own / ACMS own |
| API cost/limits | low | low | high | low |
| Human usability | native Jira | projection | both-ish | native Jira |
| Implementation complexity | low-med | low | high | **low** |

## Recommendation

**Start with Option D** (Jira parents ↔ ACMS children, one-way upward summaries):
- zero status-conflict surface; the sync is append-only comments + remote links (idempotent,
  retryable, rate-limit-friendly);
- it composes with the Phase B measurable-goal lifecycle (Jira issue = Feature/Program parent,
  ACMS child Work Items = goals with budgets);
- it preserves the human's Jira workflow untouched, satisfying "compatible with Jira-like work
  management" without surrendering ACMS authority over AI execution.

Then, if humans want Jira statuses to move automatically, **promote D → A** by adding upward
status writes behind the same adapter (a strict superset; D's link table is required by A anyway).
**Option C stays rejected** unless a customer contract explicitly demands two-way.

## Non-goals until authorized
- No Jira credentials, no connector code, no writes to Jira. Implementation would be a new
  REQ + PR after human acceptance of the authority model.
