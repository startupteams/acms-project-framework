# Handoff — PR: Work Orchestration (ACMS-REQ-007, 008, 013, 014, 036)

**Date:** 2026-09-26
**Branch:** `feat/ACMS-007-work-orchestration`
**Plan:** `docs/FIRST_EXECUTION_PLAN.md` Phase 3
**Status:** PR open, awaiting human review. Not merged.

## 1. Assigned goal and requirement IDs

Implement the Phase 3 deliverables — the minimal work-management domain:

- `ACMS-REQ-007` — hierarchical work model (product/project/feature/task with `parent_id`).
- `ACMS-REQ-008` — at most one ACTIVE primary assignment per agent (service-layer invariant + 409 on violation).
- `ACMS-REQ-013` — structured Markdown handoffs, linkable from work-item history.
- `ACMS-REQ-014` — background routine inventory (agent-reported; ACMS never schedules).
- `ACMS-REQ-036` — work item ↔ execution-task traceability (retry/resume creates new tasks, not new work items).

## 2. What changed

**Application:**

- `acms/work_models.py` — SQLAlchemy records (`work_items`, `work_assignments`, `execution_tasks`, `background_routines`, `work_handoffs`) + Pydantic create/update/response models.
- `acms/work_service.py` — service layer. The one-primary-assignment invariant lives here, not just at the API: `assign_primary()` returns `(None, 'agent-has-active-assignment')` if the agent already has an ACTIVE assignment; `close_assignment` (COMPLETED/RELEASED/SUSPENDED) is the only way to free an agent. Activating an assignment moves the work item PLANNED → ACTIVE.
- `acms/work_api.py` — 14 endpoints under `/api/v1/work` (items CRUD, assignments + close, execution tasks + finish, routine upsert/list, handoff create/list), all behind `require_admin_token` (ACMS-REQ-039).
- `acms/main.py` — router mounted.
- `migrations/versions/0002_work_orchestration.py` — hand-written migration (ADR-0007: Alembic owns schema), FKs to `agents.agent_id`, self-referencing parent with `SET NULL`.
- Version → 0.2.0.

**What is intentionally NOT here:** A2A send/steer adapter, heartbeat, SSE (Phase 4), approval engine (Phase 5), dashboard views beyond read endpoints. Out-of-scope discoveries would go to FUTURE_WORK.md — none found.

## 3. Validation

| Check | Command / Method | Result |
|---|---|---|
| Full suite | `pytest -q` | **38 passed** (29 pre-existing + 9 new) |
| Migration upgrade | `alembic upgrade head` on clean SQLite | Pass (0001 → 0002) |
| Invariant test | second assignment while ACTIVE → 409 `agent-has-active-assignment`; after COMPLETED → 201 | Pass |
| Routine upsert semantics | same (agent, name) updates one row; latest_report_at set on status report | Pass |
| Execution-task traceability | 2 tasks on 1 item, both listed by work_item_id; finish transitions RUNNING → SUCCEEDED | Pass |
| Unauthenticated work API | 401/403 on all method families | Pass |
| Secret scan | regex scan across tracked files | Clean (placeholders only) |

## 4. Blockers / assumptions

- Assignment creation is machine-token only for now; per-human-role authorization for assignments arrives with the governance phase (Phase 5, REQ-023/025) — noted in the PR.
- Execution tasks currently have no bridge wiring; `external_task_id` is a stored reference until the A2A adapter lands.

## 5. Reviewer focus

- The `assign_primary` invariant (is the 409 path airtight, including concurrent calls? — SQLite serializes; PostgreSQL row-lock noted as follow-up).
- Migration hand-check vs `work_models.py` (FK targets, nullability).
- Whether work-item status transitions should be constrained (currently any → any; left permissive for MVP).

## 6. Recommended next action

Human review of the PR. After merge, deploy to CT122 via `deploy/update.sh` and continue with Phase 4 (A2A bridge control + SSE + heartbeat).
