# ACMS Feature Slice 1 — Work Management UI (plan §13)

**Branch:** `feat/ACMS-007-work-management-ui` · **Base:** `main` (post-PR #6) · **Version:** 0.4.0 · **Tests:** 52 passed (42 + 10 new) · **No schema changes** (backend tables from PR #4)

## Requirements addressed

`ACMS-REQ-007` (hierarchical work items) · `ACMS-REQ-008` (one active primary assignment per agent — surfaced, not bypassed) · `ACMS-REQ-013` (handoffs) · `ACMS-REQ-014` (background routines inventory) · `ACMS-REQ-023` (role model) · `ACMS-REQ-036` (execution-task traceability) · `ACMS-REQ-046/049` partial (operator work view)

## What ships (vertical slice — human-visible after one deploy)

**Navigation:** `Home / Agents / Work / System` (Work added to `base.html`).

**`/ui/work` (list):** status/kind filter chips; summary cards (total/active/planned/completed); newest-first table with kind badge, status color, parent title, creator, updated timestamp. Administrator sees `+ New work item`.

**`/ui/work/new` (Administrator only):** create Product/Project/Feature/Task with optional parent, title, Markdown scope. `created_by` records the logged-in human (ACMS-REQ-010/011 provenance).

**`/ui/work/{id}` (detail):** status/parent/created/updated cards; full Markdown scope view + admin edit (title/status/scope); child-items table (hierarchy view); assignments table with the **plan §13 delivery distinction shown as a permanent notice** ("Recorded assignment only — delivery to the agent (A2A) is not implemented yet"); admin assign flow listing only agents with **no ACTIVE assignment** (REQ-008 invariant surfaced in UX); close assignment (COMPLETED/RELEASED/SUSPENDED) inline; assigned agent's background routines; execution tasks (empty state explicitly says these arrive with A2A); handoffs with add-form (admin).

**Authorization:** all reads for every mapped role; **all mutations Administrator-only** (worker/observer POSTs → 403). Worker/observer mutations return 403 without enumerating anything.

## Backend additions (additive, no migration)

- `work_service.list_assignments(..., work_item_id=...)` filter
- `work_service.list_children(db, parent_id)` (REQ-007 hierarchy view)
- `acms/ui/work_routes.py` (router, mounted in `install_ui`)
- Templates: `work_list.html`, `work_detail.html`, `work_form.html`; CSS for badges/statuses/forms

## Test results

```
pytest -q → 52 passed (42 pre-existing + 10 new tests/test_work_ui.py)
```

New coverage: login gating, list + filters, create/edit via forms, worker/observer mutation denial + read allowance, assign→detail→delivery-banner→invariant-error→close flow, unknown-agent error redirect, detail 404, parent/child + handoff rendering, created_by provenance.

## Risks / reviewer focus

- **No schema changes, no migration** — deploy-safe under plan §24 (backward compatible; old app tolerates new DB trivially since nothing changed).
- Assignment display shows the agent **display name** (human-readable); agent UUID visible in code element on the agents page.
- Handoff bodies render as pre-wrapped text (Markdown source); full MD rendering is deferred with the content-model work (slice 7).
- A2A delivery distinction is a static notice by design (plan §13) — no fabricated "Delivered/Accepted" states until slice 4 exists.

## Future work

- Per-item pagination when item counts grow (FW item if needed).
- Worker-role "my permitted work" scoping lands with REQ-009 delivery semantics (slice 4) — until then workers see all reads, same as observer.

## Next

After merge + deployment authorization: deploy via `release.sh` (this is the first schema-free release → two-stage validation exercises the fixed tooling end-to-end), then slice 2 — Agent Detail + routines (`/ui/agents/{id}`).
