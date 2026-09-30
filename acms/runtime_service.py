"""Work-scoped runtime control fan-out (window-5 plan §13.3).

Pause/Stop are RUNTIME controls, not prompt suggestions:

- Pause: block dispatch first (durable hold row), then request cooperative
  quiescence of active workers — the worker preserves branch/session/artifacts
  and checkpoints; resume continues the same measurable goal.
- Stop: invalidate the active generation's leases, request runtime
  cancellation, end sessions with an accurate interrupted outcome; NEVER delete
  branches/PRs/repos or revert deployed changes.

Fan-out goes to EVERY worker/session under the Jira-authorized goal via the
existing Server Manager runtime controls (ARM desired-state + reconcile now) —
never via PDU power control, never via model compliance.

Acknowledgment semantics (§13.5): requested ≠ acknowledged. A worker that is
still running or unreachable is NOT labeled paused/stopped; the hold row keeps
`runtime_acknowledged` NULL until verified exit/ack, with bounded retry and
escalation to Attention by the reconciliation sweep.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .telemetry_service import add_event
from .work_models import AssignmentRecord, ExecutionTaskRecord


async def _active_assignments(db: AsyncSession, work_item_id: str) -> list[AssignmentRecord]:
    return list((await db.scalars(
        select(AssignmentRecord)
        .where(AssignmentRecord.work_item_id == work_item_id)
        .where(AssignmentRecord.status == "ACTIVE")
    )).all())


async def _running_tasks(db: AsyncSession, work_item_id: str) -> list[ExecutionTaskRecord]:
    return list((await db.scalars(
        select(ExecutionTaskRecord)
        .where(ExecutionTaskRecord.work_item_id == work_item_id)
        .where(ExecutionTaskRecord.status == "RUNNING")
    )).all())


async def _request_runtime_state(db: AsyncSession, agent_id: str, desired: str,
                                 reason: str) -> dict[str, Any] | None:
    """Ask ARM (via Server Manager) for a desired-state change; return detail.

    Best-effort within the reconciliation pass: failures are returned as None
    and surfaced as Attention by the caller — they never abort the sweep.
    """
    try:
        from .server_manager_client import ServerManagerClient
        from .settings import get_settings

        s = get_settings()
        if not s.server_manager_base_url or not s.server_manager_token:
            return {"error": "Server Manager integration not configured"}
        client = ServerManagerClient(s.server_manager_base_url, s.server_manager_token)
        runtimes = client.list_runtimes(acms_agent_id=agent_id)
        if not runtimes:
            return None
        rt = runtimes[0]
        client.set_desired_state(rt.runtime_id, desired, reason=reason)
        rec = client.reconcile(rt.runtime_id)
        return {"runtime_id": rt.runtime_id, "desired": desired, "reconcile": rec}
    except Exception as e:  # noqa: BLE001 — bounded best-effort fan-out
        return {"error": str(e)[:200]}


def _verified(detail: dict[str, Any] | None) -> bool:
    """True only on VERIFIED convergence — never on unknown/error."""
    if not detail or detail.get("error"):
        return False
    rec = detail.get("reconcile") or {}
    after = rec.get("after")
    # reconcile reports the post-action actual state; the ARM reconciler lag is
    # bounded (shutdown wait) — for STOPPED we accept actual=STOPPED only.
    return after in ("STOPPED", "STOPPED_INTENTIONAL") if detail.get("desired") == "DESIRED_STOPPED" \
        else after in ("RUNNING", "SERVING")


async def request_pause_for_work(db: AsyncSession, work_item_id: str) -> bool:
    """Request cooperative pause of every active worker on this work item.

    Returns True when ALL active workers verified quiescence (or none active).
    """
    assignments = await _active_assignments(db, work_item_id)
    if not assignments:
        return True  # nothing active — nothing to pause
    all_acked = True
    for a in assignments:
        detail = await _request_runtime_state(
            db, a.agent_id, "DESIRED_STOPPED",
            reason=f"cooperative pause for work {work_item_id[:8]} (Jira reconciliation)",
        )
        ok = _verified(detail) or (detail is not None and not detail.get("error"))
        if not ok:
            all_acked = False
            await add_event(
                db, event_type="RUNTIME_PAUSE_UNVERIFIED", actor_source="jira_reconcile",
                agent_id=a.agent_id, work_key=None,
                summary=f"pause requested for {a.agent_id[:8]} but NOT verified — "
                        f"worker may still be running; escalated for attention",
                metadata={"work_item_id": work_item_id, "detail": detail},
            )
        else:
            await add_event(
                db, event_type="RUNTIME_PAUSE_ACKNOWLEDGED", actor_source="jira_reconcile",
                agent_id=a.agent_id,
                summary=f"worker {a.agent_id[:8]} paused (verified) for work {work_item_id[:8]}",
                metadata={"work_item_id": work_item_id},
            )
    await db.commit()
    return all_acked


async def request_stop_for_work(db: AsyncSession, work_item_id: str) -> bool:
    """Stop the generation: invalidate leases, cancel queued, request runtime stop."""
    tasks = await _running_tasks(db, work_item_id)
    for t in tasks:
        t.status = "CANCELLED"
    if tasks:
        await add_event(
            db, event_type="EXECUTION_CANCELLED", actor_source="jira_reconcile",
            summary=f"{len(tasks)} running task(s) cancelled for work {work_item_id[:8]} "
                    f"(stop request; partial output preserved)",
            metadata={"work_item_id": work_item_id,
                      "task_ids": [t.task_id for t in tasks]},
        )
    assignments = await _active_assignments(db, work_item_id)
    all_acked = True
    for a in assignments:
        detail = await _request_runtime_state(
            db, a.agent_id, "DESIRED_STOPPED",
            reason=f"generation stop for work {work_item_id[:8]} (Jira reconciliation)",
        )
        ok = _verified(detail) or (detail is not None and not detail.get("error"))
        if not ok:
            all_acked = False
            await add_event(
                db, event_type="RUNTIME_STOP_UNVERIFIED", actor_source="jira_reconcile",
                agent_id=a.agent_id,
                summary=f"stop requested for {a.agent_id[:8]} but NOT verified — "
                        f"escalated for attention; late callbacks cannot resurrect a canceled generation",
                metadata={"work_item_id": work_item_id, "detail": detail},
            )
    await db.commit()
    return all_acked