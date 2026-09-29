"""A2A work dispatch — the single authoritative path for starting NEW cloud
execution of a Work Item on a worker agent (REV2 Phase C, REQ-059 gate).

Design rules from the plan:
- ONE dispatch point. UI/API layers call this service; budget checks are never
  duplicated in several callers.
- Gate BEFORE new cloud execution: budget execution-check verdict decides.
  - below hard limit  → dispatch
  - soft exceeded     → dispatch + retain the warning in the event trail
  - hard exceeded     → no NEW dispatch; durable event + Attention item
                        (EXECUTION_REJECTED); in-flight work untouched
  - hard + override   → dispatch + audited override trail already exists
- Unknown cost stays UNKNOWN — never a fabricated zero (the budget engine
  itself never blocks on unknown cost; the dispatch path preserves the fact).
- Idempotency: a client-supplied idempotency key (or a stable hash of the
  work/assignment pair) maps to ONE execution task; retries return the
  original task instead of dispatching twice.
- Correlation recorded on the task + event: Work ID/key, assignment, bridge
  run id, budget-check verdict (audit trail), external A2A task id.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import budget_service, work_service
from .session_lifecycle import fail_session_for_dispatch, open_session_for_dispatch
from .bridge import BridgeError, get_bridge_for_agent
from .telemetry_models import AgentEventRecord
from .telemetry_service import add_event
from .work_models import ExecutionTaskCreate, ExecutionTaskRecord, WorkItemRecord


class DispatchError(Exception):
    """Machine-readable dispatch failure (message = stable error code)."""

    def __init__(self, code: str, http_status: int = 409, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.detail = detail or {}


def _idempotency_key(work_item_id: str, assignment_id: str | None,
                     client_key: str | None) -> str:
    """Stable dedupe key. Client key wins; else the open assignment; else work."""
    basis = client_key or assignment_id or work_item_id
    return hashlib.sha256(f"acms-dispatch:{basis}".encode()).hexdigest()[:32]


async def _find_existing_task(db: AsyncSession, dedupe_key: str) -> Any | None:
    """The most recent task created from this dedupe key.

    The dedupe ledger lives in the durable EXECUTION_DISPATCHED event metadata
    (``dedupe_key`` → ``task_id``) — external_task_id now carries the A2A run
    id (ADR-0012 callback binding), so it can no longer double as the idem-
    potency ledger. The event log is the same durability class ACMS already
    relies on for reassignment counting (PR #32 pattern).
    """
    rows = (await db.scalars(
        select(AgentEventRecord)
        .where(AgentEventRecord.event_type == "EXECUTION_DISPATCHED")
        .order_by(AgentEventRecord.timestamp.desc())
        .limit(200)
    )).all()
    for ev in rows:  # newest first
        try:
            meta = json.loads(ev.metadata_json) if ev.metadata_json else {}
        except (TypeError, ValueError):
            continue
        if meta.get("dedupe_key") == dedupe_key and meta.get("task_id"):
            return await db.get(ExecutionTaskRecord, meta["task_id"])
    return None


async def dispatch_work(db: AsyncSession, *, work_item_id: str,
                        instruction: str, agent_id: str | None = None,
                        idempotency_key: str | None = None,
                        session_id: str | None = None,
                        actor: str = "api") -> dict[str, Any]:
    """Dispatch NEW cloud execution for a Work Item — the ONLY path that does.

    Returns a dict:
      dispatched: bool          — False when the gate blocked or dedupe hit
      reason: str               — dispatched | hard_budget_exceeded | duplicate
      task_id / external_task_id — correlation ids
      budget_check: dict        — the verdict used for the gate (audit trail)
    Raises DispatchError for structural failures (unknown work/agent/bridge).
    """
    # ---- 1. resolve the work item (+ open assignment) ----------------------
    work = await db.get(WorkItemRecord, work_item_id)
    if work is None:
        raise DispatchError("unknown-work-item", 404)
    assignments = await work_service.list_assignments(db, work_item_id=work_item_id)
    active = next((a for a in assignments if a.status == "ACTIVE"), None)
    if agent_id is None and active is None:
        raise DispatchError("no-active-assignment", 409)
    target_agent = agent_id or active.agent_id
    if active is not None and agent_id is not None and agent_id != active.agent_id:
        raise DispatchError("agent-mismatch", 409,
                            {"active_assignment_agent": active.agent_id})

    # ---- 2. idempotency: a retry must never double-dispatch ----------------
    dedupe_key = _idempotency_key(work_item_id, active.assignment_id if active else None,
                                  idempotency_key)
    existing = await _find_existing_task(db, dedupe_key)
    if existing is not None and existing.status == "RUNNING":
        return {"dispatched": False, "reason": "duplicate",
                "task_id": existing.task_id, "external_task_id": None,
                "budget_check": None}

    # ---- 3. budget gate (REQ-059) BEFORE any cloud execution ---------------
    verdict = await budget_service.check_new_execution_allowed(db, work_item_id)
    if not verdict.get("allowed", True):
        # hard exceeded without override → durable rejection event (Attention
        # picks it up from the log; no separate mutable state) + hold dispatch.
        await add_event(
            db,
            event_type="EXECUTION_REJECTED",
            actor_source="dispatch",
            agent_id=target_agent,
            work_key=work.work_key,
            assignment_key=active.assignment_key if active else None,
            summary=("NEW cloud execution blocked: hard budget exceeded on "
                     f"{work.work_key or work_item_id[:8]} "
                     f"(state={verdict.get('budget_state')}; "
                     f"override={'active' if (verdict.get('override') or {}).get('active') else 'none'})")[:512],
            metadata={"budget_check": verdict, "dispatch_attempt": True,
                      "work_item_id": work_item_id, "dedupe_key": dedupe_key},
        )
        await db.commit()
        return {"dispatched": False, "reason": "hard_budget_exceeded",
                "task_id": None, "external_task_id": None,
                "budget_check": verdict}

    # ---- 4. resolve the bridge target --------------------------------------
    bridge = get_bridge_for_agent(target_agent)
    if bridge is None:
        raise DispatchError("no-bridge-target", 503, {"agent_id": target_agent})

    # ---- 4b. pre-create the execution task so the runtime can call back ----
    # ADR-0012: the completion callback binds task_id + agent id, so the task
    # row must exist BEFORE the run starts and its id must travel with the
    # instruction. Failure to record the task aborts the dispatch (nothing
    # was sent to the bridge — budget not consumed, no session created).
    task, err = await work_service.create_execution_task(db, ExecutionTaskCreate(
        work_item_id=work_item_id,
        agent_id=target_agent,
        external_task_id=None,  # a2a run id back-filled after send_work
    ))
    if task is None:
        raise DispatchError(err or "task-record-failed", 500)

    # ADR-0012 primary completion path: the instruction carries the callback
    # contract so the runtime reports its terminal result itself. The
    # reconcile sweep stays the fallback for lost/late callbacks.
    from .settings import get_settings

    callback_base = get_settings().callback_base_url or ""
    dispatch_instruction = instruction
    if callback_base:
        dispatch_instruction = (
            f"{instruction}\n\n---\nCOMPLETION PROTOCOL (ACMS ADR-0012): when this task "
            f"reaches a terminal state, POST to "
            f"{callback_base}/api/v1/callbacks/execution-completion with header "
            f"\"Authorization: Bearer <ACMS_CALLBACK_TOKEN>\" and JSON body "
            f'{{"task_id": "{task.task_id}", "acms_agent_id": "{target_agent}", '
            f'"status": "SUCCEEDED"|"FAILED"|"CANCELLED", "completed_at": "<UTC ISO>", '
            f'"result_reference": "<PR/artifact URL>", "error_summary": "<one line when failed>"}}. '
            f"Your agent id for acms_agent_id is {target_agent}. This is mandatory; "
            f"ACMS reconciliation only repairs lost callbacks."
        )

    # ---- 5. dispatch through the bridge (new A2A run) ----------------------
    session_for_run = None  # set on success; failure path closes it if set
    try:
        result = bridge.send_work(
            work_key=work.work_key or work_item_id,
            assignment_key=(active.assignment_key if active else "") or "",
            instruction=dispatch_instruction,
            session_id=session_id,
        )
    except BridgeError as e:
        # dispatch itself failed — record it; budget NOT consumed (ACMS did not
        # create a session), retry after the error is resolved.
        await add_event(
            db,
            event_type="EXECUTION_FAILED",
            actor_source="dispatch",
            agent_id=target_agent,
            work_key=work.work_key,
            assignment_key=active.assignment_key if active else None,
            summary=f"A2A dispatch failed: {str(e)[:180]}",
            metadata={"dedupe_key": dedupe_key, "budget_check": verdict,
                      "work_item_id": work_item_id},
        )
        await fail_session_for_dispatch(db, session=session_for_run, reason=str(e))
        await db.commit()
        raise DispatchError("bridge-error", 502, {"detail": str(e)[:300]}) from None

    run_id = (result or {}).get("run_id") or (result or {}).get("id")

    # back-fill the A2A run id on the pre-created task (ADR-0012 binding)
    if run_id:
        task.external_task_id = str(run_id)

    # Phase F (plan §8): the dispatch automatically owns an ExecutionSession.
    # Idempotent on the A2A run id — a duplicate dispatch never duplicates it.
    session_for_run = await open_session_for_dispatch(
        db, agent_id=str(target_agent), work_item_id=work_item_id,
        assignment_id=(active.assignment_id if active else None),
        a2a_task_id=str(run_id) if run_id else None,
        harness_session_id=(result or {}).get("session_id"),
    )

    # ---- 6. execution task pre-created in step 4b (ADR-0012); the dedupe
    # ledger is preserved on the event metadata (``disp-<key>`` remains the
    # client-visible idempotency marker) ------------------------

    # ---- 7. durable audit trail --------------------------------------------
    await add_event(
        db,
        event_type="EXECUTION_DISPATCHED",
        actor_source="dispatch",
        agent_id=target_agent,
        work_key=work.work_key,
        assignment_key=active.assignment_key if active else None,
        a2a_task_id=str(run_id) if run_id else None,
        summary=(f"Dispatched [{work.work_key or work_item_id[:8]}] to {str(target_agent)[:8]}"
                 + (f" (soft exceeded: {verdict.get('budget_state')})"
                    if verdict.get("budget_state") == "SOFT_EXCEEDED" else ""))[:512],
        metadata={"run_id": run_id, "budget_check": verdict,
                  "dedupe_key": dedupe_key, "task_id": task.task_id,
                  "assignment_id": active.assignment_id if active else None,
                  "work_item_id": work_item_id, "actor": actor},
    )
    await db.commit()
    return {"dispatched": True, "reason": verdict.get("reason", "dispatched"),
            "task_id": task.task_id, "external_task_id": str(run_id) if run_id else None,
            "budget_check": verdict}
