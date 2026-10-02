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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import budget_service, work_service
from .session_lifecycle import fail_session_for_dispatch, open_session_for_dispatch
from .bridge import BridgeError, get_bridge_for_agent
from .telemetry_models import AgentEventRecord
from .telemetry_service import add_event
from .work_models import ExecutionTaskCreate, ExecutionTaskRecord, WorkItemRecord


ASSIGNMENT_SCHEMA_VERSION = "acms-a2a-assignment-v1"


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


async def _check_worker_busy(db: AsyncSession, agent_id: str, work_item_id: str) -> dict | None:
    """One task per worker (plan §3; ADR-0013): refuse a NEW dispatch when the
    target agent already has a non-terminal execution task for a DIFFERENT
    work item. Returns the blocking detail or None."""
    rows = (await db.scalars(
        select(ExecutionTaskRecord)
        .where(ExecutionTaskRecord.agent_id == agent_id,
               ExecutionTaskRecord.status == "RUNNING")
        .order_by(ExecutionTaskRecord.started_at.desc())
        .limit(10)
    )).all()
    for t in rows:
        if t.work_item_id != work_item_id:
            return {
                "blocking_work_item_id": t.work_item_id,
                "blocking_task_id": t.task_id,
                "blocking_since": t.started_at.isoformat() if t.started_at else None,
            }
    return None


def _build_assignment_envelope(
    *, work: WorkItemRecord, agent_id: str, task_id: str, assignment_id: str | None,
    session_id: str | None, jira_issue_key: str | None, project: dict | None,
    product: dict | None, sprint: dict | None, tags: list[str],
    instruction: str, completion_condition: str | None,
    output_artifacts_expected: list[str], model_policy: dict, priority: str,
    callback_base: str, callback_token_hint: str, idempotency_key: str,
) -> dict[str, Any]:
    """acms-a2a-assignment-v1 envelope (ADR-0013; plan §12). project may never
    be null; product/sprint may be."""
    return {
        "schema_version": ASSIGNMENT_SCHEMA_VERSION,
        "assignment_id": assignment_id,
        "idempotency_key": idempotency_key,
        "agent_id": str(agent_id),
        "work_item_id": work.work_item_id,
        "execution_task_id": task_id,
        "execution_session_id": session_id,
        "work_key": work.work_key,
        "jira_issue_key": jira_issue_key,
        "project": project,
        "product": product,
        "sprint": sprint,
        "tags": tags,
        "instructions": instruction,
        "completion_condition": completion_condition,
        "output_artifacts_expected": output_artifacts_expected,
        "model_policy": model_policy,
        "priority": priority,
        "callback_urls": {
            "heartbeat": f"{callback_base}/api/v1/fleet/agents/{agent_id}/heartbeat" if callback_base else None,
            "events": f"{callback_base}/api/v1/execution/{task_id}/stream" if callback_base else None,
            "completion": f"{callback_base}/api/v1/callbacks/execution-completion" if callback_base else None,
            "artifacts": f"{callback_base}/api/v1/artifacts" if callback_base else None,
            "human_attention": f"{callback_base}/api/v1/inbox" if callback_base else None,
        },
        "callback_token_hint": callback_token_hint,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


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

    # ---- 2a. one-task-per-worker (plan §3; ADR-0013) ------------------------
    busy = await _check_worker_busy(db, str(target_agent), work_item_id)
    if busy is not None:
        await add_event(
            db,
            event_type="EXECUTION_REJECTED",
            actor_source="dispatch",
            agent_id=target_agent,
            work_key=work.work_key,
            assignment_key=active.assignment_key if active else None,
            summary=(f"409 WORKER_BUSY: {str(target_agent)[:8]} already executes "
                     f"{busy['blocking_work_item_id'][:8]} — scheduler must pick "
                     f"another idle worker")[:512],
            metadata={"worker_busy": busy, "work_item_id": work_item_id,
                      "dedupe_key": dedupe_key},
        )
        await db.commit()
        return {"dispatched": False, "reason": "worker_busy", "worker_busy": busy,
                "task_id": None, "external_task_id": None, "budget_check": None}

    # ---- 2b. JIRA KICKOFF GATE (window-5 §7/§13) BEFORE budget -------------
    # Server-enforced on EVERY dispatch path (UI, API, scheduler, retry,
    # resume, Executive decomposition, product bootstrap — all converge here).
    # Live Jira re-read immediately before the decision (§13.4 step 5).
    from .jira_gate import (ELIGIBLE, JiraGateError, gate_work_item)

    try:
        gate = await gate_work_item(db, work_item_id, live_check=True)
    except JiraGateError as e:
        raise DispatchError("jira-gate-error", 500, {"detail": str(e)[:200]}) from None
    if not gate.eligible:
        await add_event(
            db,
            event_type="EXECUTION_REJECTED",
            actor_source="dispatch",
            agent_id=target_agent,
            work_key=work.work_key,
            assignment_key=active.assignment_key if active else None,
            summary=(f"NEW execution blocked by Jira kickoff gate on "
                     f"{work.work_key or work_item_id[:8]}: {gate.verdict} — "
                     f"{gate.reason[:300]}")[:512],
            metadata={"gate_verdict": gate.to_dict(), "work_item_id": work_item_id,
                      "dedupe_key": dedupe_key, "gate": "jira"},
        )
        await db.commit()
        return {"dispatched": False, "reason": f"jira_gate_{gate.verdict.lower()}",
                "task_id": None, "external_task_id": None,
                "budget_check": None, "jira_gate": gate.to_dict()}

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

    # ---- 4b. resolve model policy (ADR-0015) + pre-create the task -----------
    # The completion callback binds task_id + agent id, so the task row must
    # exist BEFORE the run starts and its id must travel with the instruction.
    from .model_policy import resolve_policy
    from .settings import get_settings

    resolved_policy = await resolve_policy(db, work_item_id, str(target_agent))
    from datetime import datetime as _dt, timedelta as _td

    ttft_deadline = _dt.now(timezone.utc) + _td(
        seconds=get_settings().model_ttft_timeout_seconds)

    task, err = await work_service.create_execution_task(db, ExecutionTaskCreate(
        work_item_id=work_item_id,
        agent_id=target_agent,
        external_task_id=None,  # a2a run id back-filled after send_work
        transport_state="DISPATCHING",
        assignment_id=(active.assignment_id if active else None),
        idempotency_key=dedupe_key,
        model_policy_json=json.dumps(resolved_policy),
        effective_model=resolved_policy.get("preferred_model"),
        model_resolution_reason=resolved_policy.get("resolution_reason"),
        ttft_deadline_at=ttft_deadline,
    ))
    if task is None:
        raise DispatchError(err or "task-record-failed", 500)

    callback_base = get_settings().callback_base_url or ""
    project_ctx: dict | None = None
    if work.project_id:
        from .a2a_models import ProjectRecord

        proj = await db.get(ProjectRecord, work.project_id)
        if proj is not None:
            project_ctx = {"id": proj.project_id, "name": proj.name}
    envelope = _build_assignment_envelope(
        work=work,
        agent_id=str(target_agent),
        task_id=task.task_id,
        assignment_id=(active.assignment_id if active else None),
        session_id=None,  # session id is assigned after the run is accepted
        jira_issue_key=work.jira_issue_key,
        project=project_ctx,
        product=None,   # product context rides the project; §12 allows null
        sprint=None,
        tags=[],
        instruction=instruction,
        completion_condition=None,
        output_artifacts_expected=[],
        model_policy=resolved_policy,
        priority="normal",
        callback_base=callback_base,
        callback_token_hint="<ACMS_CALLBACK_TOKEN — never embedded; scoped token on the worker>",
        idempotency_key=dedupe_key,
    )

    # ---- 4c. MCP assignment-token auto-mint (plan §12/§13; W2) --------------
    # Best-effort, NEVER dispatch-blocking. On success the envelope gains an
    # mcp_assignment block {gateway_url, assignment_token, expires_at, ...};
    # the raw token rides the envelope/instruction to the worker exactly once
    # (never logged, never persisted in ACMS). On failure the envelope still
    # advertises the gateway (when configured) with token=None + a warning
    # event, so workers know MCP exists but this run rides without a token.
    from .settings import get_settings as _gs

    _s = _gs()
    mcp_block: dict[str, Any] | None = None
    if _s.mcp_gateway_base_url:
        mcp_block = {"gateway_url": _s.mcp_gateway_base_url.rstrip("/"),
                     "assignment_token": None, "token_id": None, "expires_at": None}
        if _s.mcp_dispatch_mint_enabled and _s.mcp_gateway_internal_token:
            from .mcp_gateway_client import McpGatewayClient

            _agent_name = None
            if target_agent:
                from .registry import list_agents as _list_agents

                try:
                    _agents = {a.agent_id: a for a in await _list_agents(db)}
                    _agent = _agents.get(str(target_agent))
                    _agent_name = (_agent.display_name if _agent else None)
                except Exception:  # noqa: BLE001 — name lookup is best-effort
                    _agent_name = None
            if _agent_name:
                _minted = McpGatewayClient().mint_assignment(
                    agent_name=_agent_name,
                    work_uid=work.work_uid or work.work_key or work_item_id,
                    ttl_hours=max(1, int(_s.mcp_assignment_ttl_hours)),
                    jira_issue_key=work.jira_issue_key,
                    project_slug=project_ctx.get("id") if project_ctx else None,
                )
                if _minted is not None:
                    mcp_block.update({
                        "assignment_token": _minted.get("token"),
                        "token_id": _minted.get("token_id"),
                        "expires_at": _minted.get("expires_at"),
                    })
                else:
                    await add_event(
                        db,
                        event_type="MCP_ASSIGNMENT_MINT_FAILED",
                        actor_source="dispatch",
                        agent_id=str(target_agent),
                        work_key=work.work_key,
                        summary=f"MCP assignment-token mint failed for "
                                f"{work.work_key or work_item_id[:8]} (dispatch proceeds)",
                        metadata={"work_item_id": work_item_id,
                                  "dedupe_key": dedupe_key},
                    )
    if mcp_block is not None:
        envelope["mcp_assignment"] = mcp_block

    dispatch_instruction = (
        f"{instruction}\n\n---\nACMS ASSIGNMENT ENVELOPE (machine-readable; "
        f'schema {ASSIGNMENT_SCHEMA_VERSION}):\n'
        f"{json.dumps(envelope, indent=1)}\n\n---\nCOMPLETION PROTOCOL (ACMS ADR-0012): when this task "
        f"reaches a terminal state, POST to "
        f"{callback_base}/api/v1/callbacks/execution-completion with header "
        f"\"Authorization: Bearer <ACMS_CALLBACK_TOKEN>\" and JSON body "
        f'{{"task_id": "{task.task_id}", "acms_agent_id": "{target_agent}", '
        f'"status": "SUCCEEDED"|"FAILED"|"CANCELLED", "completed_at": "<UTC ISO>", '
        f'"error_summary": "<one line when failed>"}}. '
        f"Your agent id for acms_agent_id is {target_agent}. "
        f"ACMS runtime infrastructure also observes completion and delivers "
        f"the callback itself — this hint is a redundant optional path."
    )

    # ---- 5. dispatch through the bridge (new A2A run) ----------------------
    session_for_run = None  # set on success; failure path closes it if set
    try:
        result = bridge.send_work(
            work_key=work.work_key or work_item_id,
            assignment_key=(active.assignment_key if active else "") or "",
            instruction=dispatch_instruction,
            session_id=session_id,
            model=task.effective_model,
        )
    except BridgeError as e:
        # dispatch itself failed — record it; budget NOT consumed (ACMS did not
        # create a session), retry after the error is resolved. The task row
        # rolls back to DISPATCHING-was-sent state: mark FAILED so the worker
        # lock clears and the retry creates a fresh task.
        task.status = "FAILED"
        task.finished_at = datetime.now(timezone.utc)
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
        from .inbox_service import create_inbox_item

        await create_inbox_item(
            db, title=f"Dispatch failed on {work.work_key or work_item_id[:8]}",
            summary="Dispatch to worker failed. Retry when ready.",
            item_class="ACTION_REQUIRED", severity="medium",
            agent_id=str(target_agent), work_item_id=work_item_id,
            jira_issue_key=work.jira_issue_key,
            correlation_id=f"dispatch-failed:{dedupe_key}",
            metadata={"bridge_error": str(e)[:300], "actor": actor},
        )
        await fail_session_for_dispatch(db, session=session_for_run, reason=str(e))
        await db.commit()
        raise DispatchError("bridge-error", 502, {"detail": str(e)[:300]}) from None

    run_id = (result or {}).get("run_id") or (result or {}).get("id")

    # ACK: the run id IS the worker's acceptance receipt (ADR-0013) — the
    # Hermes api-server accepted the run and returned 202 with run_id.
    if run_id:
        task.external_task_id = str(run_id)
        task.transport_state = "RUNNING"

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

    # ---- 8. runtime-driven completion watcher (ADR-0012 primary path) ------
    # Infrastructure observes the bridge run and delivers the completion
    # callback itself — never dependent on the model following instructions
    # (window-4 plan §C1). Scheduled AFTER commit; failures never break the
    # dispatch result (watch scheduling is fail-open, reconcile is fallback).
    if run_id:
        from .run_watcher import schedule_run_watch
        from .run_event_pump import schedule_run_event_pump

        schedule_run_watch(task.task_id)
        # live activity visibility (ADR-0014) — fail-open, never breaks dispatch
        schedule_run_event_pump(task.task_id)

    return {"dispatched": True, "reason": verdict.get("reason", "dispatched"),
            "task_id": task.task_id, "external_task_id": str(run_id) if run_id else None,
            "budget_check": verdict}
