"""ADR-0012 — worker completion callback (Accepted 2026-09-29).

Canonical completion architecture:
    Bridge/worker completion callback = PRIMARY immediate completion signal
    ACMS reconciliation sweep          = FALLBACK / repair mechanism

A callback carries durable semantic data only:
    task/run id, terminal status (SUCCEEDED|FAILED|CANCELLED),
    completed_at, result/handoff reference, usage/cost references when
    available, concise error summary when failed.
It NEVER carries full transcripts / token streams / verbose reasoning.

Identity binding (fail-closed): the callback must present
    acms_agent_id + execution task id (+ a2a run id when known)
and the recorded task must already belong to that agent. A runtime cannot
close another runtime's task. Unknown task ids are REJECTED with an audit
event; wrong agent identity fails CLOSED with an audit event.

Idempotency/authority:
    duplicate callback        → harmless (idempotent success, no dup close)
    late callback after close → harmless (idempotent)
    unknown task id           → 404 + audit event
    wrong agent/runtime       → 403 fail-closed + audit event
    lost callback             → reconciliation eventually repairs state
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .memory_models import ExecutionSessionRecord
from .telemetry_service import add_event
from .work_models import ExecutionTaskRecord

TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")


class CompletionCallbackError(Exception):
    """Machine-readable callback failure (message = stable error code)."""

    def __init__(self, code: str, http_status: int = 409, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.detail = detail or {}


class CompletionCallbackRequest(BaseModel):
    """Durable semantic payload only — never transcripts or token streams."""

    task_id: str = Field(max_length=64)
    acms_agent_id: str = Field(max_length=36)
    a2a_run_id: str | None = Field(default=None, max_length=255)
    status: str = Field(max_length=16)
    completed_at: datetime | None = None
    result_reference: str | None = Field(default=None, max_length=512)
    handoff_reference: str | None = Field(default=None, max_length=512)
    error_summary: str | None = Field(default=None, max_length=512)
    usage_reference: dict | None = None


async def process_completion_callback(db: AsyncSession, payload: CompletionCallbackRequest) -> dict[str, Any]:
    """Validate identity, close the task + session atomically, audit the fact.

    Returns a dict with ``result`` in {closed, already_terminal} — both are
    idempotent successes. Raises CompletionCallbackError for structural
    failures (unknown task, identity mismatch, invalid status).
    """
    if payload.status not in TERMINAL_STATUSES:
        raise CompletionCallbackError("invalid-status", 422,
                                      {"allowed": list(TERMINAL_STATUSES)})

    task = await db.get(ExecutionTaskRecord, payload.task_id)
    if task is None:
        # Unknown task id: reject + audit (ADR-0012 §1.8). No task row to
        # correlate to, so the audit event carries the claimed ids only.
        await add_event(
            db, event_type="EXECUTION_CALLBACK_REJECTED", actor_source="completion_callback",
            agent_id=None, work_key=None,
            summary=f"Completion callback rejected: unknown task id {payload.task_id[:24]}",
            metadata={"claimed_agent_id": payload.acms_agent_id,
                      "a2a_run_id": payload.a2a_run_id, "reason": "unknown-task-id"},
        )
        await db.commit()
        raise CompletionCallbackError("unknown-task-id", 404)

    # ---- identity binding: fail closed on agent mismatch --------------------
    task_agent = task.agent_id or ""
    if task_agent != payload.acms_agent_id:
        await add_event(
            db, event_type="EXECUTION_CALLBACK_REJECTED", actor_source="completion_callback",
            agent_id=task.agent_id, work_key=None,
            summary="Completion callback rejected: agent identity mismatch (fail closed)",
            metadata={"task_id": payload.task_id, "recorded_agent_id": task_agent,
                      "claimed_agent_id": payload.acms_agent_id, "reason": "agent-mismatch"},
        )
        await db.commit()
        raise CompletionCallbackError("agent-mismatch", 403,
                                      {"task_agent_id": task_agent or None})

    # ---- already terminal? idempotent no-op ---------------------------------
    if task.status != "RUNNING":
        return {"result": "already_terminal", "task_id": task.task_id,
                "task_status": task.status, "session_id": None}

    completed_at = payload.completed_at or datetime.now(timezone.utc)
    task.status = payload.status
    task.finished_at = completed_at

    # ---- close the correlated ExecutionSession (no manual close) -----------
    session = (await db.scalars(
        select(ExecutionSessionRecord)
        .where(ExecutionSessionRecord.a2a_task_id == (payload.a2a_run_id or task.external_task_id or ""))
        .where(ExecutionSessionRecord.status == "OPEN")
        .order_by(ExecutionSessionRecord.started_at.desc())
        .limit(1)
    )).first()
    if session is None and task.external_task_id:
        session = (await db.scalars(
            select(ExecutionSessionRecord)
            .where(ExecutionSessionRecord.a2a_task_id == task.external_task_id)
            .where(ExecutionSessionRecord.status == "OPEN")
            .order_by(ExecutionSessionRecord.started_at.desc())
            .limit(1)
        )).first()

    session_id = None
    if session is not None:
        session.status = "CLOSED"
        session.ended_at = completed_at
        if payload.result_reference:
            session.handoff_id = None  # result/handoff refs live in the event metadata
        session_id = session.session_id

    # ---- durable semantic event (Attention policy applied downstream) ------
    event_type = {
        "SUCCEEDED": "EXECUTION_COMPLETED",
        "FAILED": "EXECUTION_FAILED",
        "CANCELLED": "EXECUTION_CANCELLED",
    }[payload.status]
    summary_bits = [f"Completion callback: task {payload.task_id[:12]} → {payload.status}"]
    if payload.error_summary:
        summary_bits.append(f"error: {payload.error_summary[:120]}")
    await add_event(
        db, event_type=event_type, actor_source="completion_callback",
        agent_id=task.agent_id, work_key=None,
        a2a_task_id=payload.a2a_run_id,
        summary="; ".join(summary_bits)[:512],
        metadata={
            "task_id": payload.task_id, "status": payload.status,
            "session_id": session_id, "source": "adr0012_callback",
            "result_reference": payload.result_reference,
            "handoff_reference": payload.handoff_reference,
            "usage_reference": payload.usage_reference,
            "error_summary": payload.error_summary,
        },
    )
    # ---- Inbox auto-conversion (STEA-004 plan §23) + usage capture (§26) ----
    # When the execution resolves, unresolved ACTION_REQUIRED inbox items for
    # this work item demote to STALE (action became irrelevant), and the run's
    # token usage is recorded for cost attribution.
    try:
        from .inbox_service import demote_for_work_item

        await demote_for_work_item(db, task.work_item_id,
                                   to_class="STALE",
                                   reason=f"execution {payload.status.lower()}")
    except Exception:  # noqa: BLE001 — inbox hygiene never breaks completion
        pass
    try:
        from .economics_service import record_execution_usage

        await record_execution_usage(
            db, task=task, session_id=session_id,
            usage=payload.usage_reference or {}, completed_at=completed_at)
    except ImportError:
        pass  # economics usage capture ships with the §26 pipeline
    except Exception:  # noqa: BLE001 — usage capture never breaks completion
        pass

    # ---- canonical work handoff (STEA-004 plan §9) ---------------------------
    # Every terminal Work Item gets exactly one canonical Markdown handoff.
    # Never breaks completion (swallows + audits internally).
    try:
        from .canonical_handoff import generate_canonical_handoff

        await generate_canonical_handoff(
            db, task=task, status=payload.status, completed_at=completed_at,
            error_summary=payload.error_summary,
            usage_reference=payload.usage_reference,
            handoff_reference=payload.handoff_reference,
        )
    except Exception:  # noqa: BLE001 — belt & braces around the never-raise helper
        pass
    await db.commit()
    return {"result": "closed", "task_id": task.task_id,
            "task_status": task.status, "session_id": session_id}