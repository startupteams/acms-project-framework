# ADR-0017: Projects/Products/Tags, Jira reconciliation controls, Human Inbox, and Artifacts

**Status:** Proposed (human direction: STEA-004 execution plan 2026-10-01 §6/§9–§11/§23–§25)
**Date:** 2026-10-01

## Context

Work Items have no Project/Product home; Jira status changes don't control
downstream execution; the Attention feed is a derived list without read/unread
or reply actions; handoff Markdown has no durable home.

## Decision

### Data model (one migration, 0012)

- `products` (id, name, slug unique, business_summary, status, default_model_policy JSON, context_notes, timestamps)
- `projects` (id, name, slug unique, product_id FK nullable, repos TEXT, jira_project_key, default_model_policy JSON, timestamps)
- `work_items.project_id` FK → projects (NULL allowed for legacy rows; new items require it — enforced at API layer)
- `tags` (id, name unique) + `tag_links` (tag_id, scope: work_item|project|product, scope_id)
- `artifacts` (id, work_item_id FK, execution_session_id nullable, agent_id, project_id, product_id nullable, jira_issue_key, artifact_type, title, bluf, mime_type, content TEXT, sha256, created_at/by)
- `human_inbox_items` (id, class: ACTION_REQUIRED|FYI|STALE, severity, title, summary, agent_id, work_item_id, execution_session_id, project_id, product_id, jira_issue_key, correlation_id unique-dedupe, read_at, archived_at, created_at, updated_at, metadata_json)
- `execution_events` (per ADR-0014 live stream persistence)
- `jira_reconcile_actions` stays audit-only (existing event log is the ledger;
  no second store).

### Jira status → execution control (plan §6)

The reconciliation service (window-5 `jira_reconcile.start_reconciliation_run`)
gains an ACTION MAP applied AFTER linking, BEFORE dispatch eligibility:

| Jira status | ACMS effect |
|---|---|
| IDEA/UNVALIDATED | not dispatchable (existing ELIGIBLE gate already covers) |
| TO START | dispatchable (existing ready status) |
| IN PROGRESS | dispatchable; running work continues |
| BLOCKED | new dispatch refused; if running → `WORK_HOLD_REQUESTED` event + Inbox ACTION_REQUIRED (steer/stop decision is human-safe default: request safe stop via steer message; never force-kill) |
| DEFERRED | same hold behavior; context preserved; no new execution |
| IN REVIEW | no NEW implementation dispatch; completed output preserved |
| COMPLETED | terminal; no new execution |

- Mapping verdicts persist on `work_items.jira_eligibility` (+reason) exactly
  like the existing gate vocab, so the UI/board render one source.
- ACMS writes Jira comments/Artifact links after verified completion. Status
  transitions remain fail-closed behind `jira_status_mutation_enabled=false`;
  when explicitly enabled, ACMS may transition an ACMS-linked issue to
  `IN PROGRESS` after bridge ACK and to `IN REVIEW` only after successful
  terminal execution plus canonical Artifact creation (ACMS-REQ-063). Other
  transitions remain human-controlled.

### Products/Projects UI (plan §11)

Replace `+ New Product/Idea` ambiguity: `/ui/products` gets `+ Create Product`
and `+ Create Project` with one-line definitions; Product view shows related
project chips (removable), work counts by execution state, Jira issues,
context notes; Project view shows product, repos, Jira mapping, active work,
artifacts, inbox items.

### Human Inbox (plan §23/§24)

- `/ui/inbox` replaces `/ui/attention` nav label (feed API stays as derivation
  source; Inbox adds durable rows).
- Read/unread/mark-unread; Archive with force-warning for unresolved
  ACTION_REQUIRED; auto-demotion ACTION_REQUIRED → FYI/STALE when the linked
  execution resolves (done in inbox service on completion callback).
- Reply actions: `steer` (bridge steer to the bound session — existing tested
  endpoint), `incorporate` (append durable context to work item scope +
  optional Jira comment), `reply-only` (record), `archive`.
- Context Markdown button: on-demand generation → stored as an Artifact.
- Caveman-style short summaries required on creation (≤120 chars).
- Rate guardrail: related events within a window aggregate into one item
  (correlation_id dedupe); never suppress critical safety items.

### Artifacts (plan §25)

- Artifact subsystem: DB-authoritative Markdown (`artifacts.content`), UI at
  `/ui/artifacts/{id}` + API list/get/download.
- Jira output: description `Output Artifacts` gains
  `ACMS Handoff: https://10.0.20.122/ui/artifacts/<id>`; a short BLUF comment
  (BLUF / what changed / result / test evidence / link) — never raw Markdown
  dumps.
- Artifact creation is authenticated: agent callbacks (completion/artifact
  upload) bind to the task's agent; UI/API use session/bearer per existing
  patterns.

## Consequences

- All §39 test-matrix items for Jira/Inbox/Artifacts get direct table/service
  coverage; no behavior lives only in templates.
- Legacy work items without `project_id` keep working (nullable), flagged in
  UI as "unassigned project".