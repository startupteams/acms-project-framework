"""Work orchestration service layer (ACMS-REQ-007/008/013/014/036).

Rules enforced here (not just at the API):
- One ACTIVE primary assignment per agent (ACMS-REQ-008). Assigning new primary
  work requires the previous assignment to be COMPLETED/RELEASED/SUSPENDED first.
- Work item hierarchy: parent must exist; any kind may nest.
- Routines are inventory-only: upsert by (agent_id, name); ACMS never schedules.
- Execution tasks always trace to a work item (ACMS-REQ-036).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AgentRecord
from .work_models import (
    AssignmentCreate,
    AssignmentRecord,
    BackgroundRoutineRecord,
    ExecutionTaskCreate,
    ExecutionTaskRecord,
    HandoffCreate,
    HandoffRecord,
    RoutineUpsert,
    WorkItemCreate,
    WorkItemRecord,
    WorkItemStatus,
    WorkItemUpdate,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- work items


async def create_work_item(db: AsyncSession, payload: WorkItemCreate) -> WorkItemRecord:
    if payload.parent_id is not None:
        parent = await db.get(WorkItemRecord, payload.parent_id)
        if parent is None:
            raise ValueError(f"parent work item {payload.parent_id} does not exist")
    now = _now()
    record = WorkItemRecord(
        work_item_id=_new_id(),
        parent_id=payload.parent_id,
        kind=payload.kind.value,
        title=payload.title,
        created_by=payload.created_by,
        status=WorkItemStatus.PLANNED.value,
        scope_markdown=payload.scope_markdown,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_work_item(db: AsyncSession, work_item_id: str) -> WorkItemRecord | None:
    return await db.get(WorkItemRecord, work_item_id)


async def list_work_items(db: AsyncSession) -> list[WorkItemRecord]:
    result = await db.scalars(select(WorkItemRecord).order_by(WorkItemRecord.created_at))
    return list(result.all())


async def update_work_item(
    db: AsyncSession, work_item_id: str, payload: WorkItemUpdate
) -> WorkItemRecord | None:
    record = await db.get(WorkItemRecord, work_item_id)
    if record is None:
        return None
    if payload.status is not None:
        record.status = payload.status.value
    if payload.title is not None:
        record.title = payload.title
    if payload.scope_markdown is not None:
        record.scope_markdown = payload.scope_markdown
    record.updated_at = _now()
    await db.commit()
    await db.refresh(record)
    return record


# ---------------------------------------------------------------- assignments


async def assign_primary(
    db: AsyncSession, payload: AssignmentCreate, assigned_by: str | None = None
) -> tuple[AssignmentRecord | None, str | None]:
    """Create a primary assignment, enforcing the ACMS-REQ-008 invariant.

    Returns (record, None) on success, or (None, error_code) where error_code is
    one of: 'unknown-agent' | 'unknown-work-item' | 'work-item-not-open' |
    'agent-has-active-assignment'.
    """
    agent = await db.get(AgentRecord, payload.agent_id)
    if agent is None:
        return None, "unknown-agent"
    work_item = await db.get(WorkItemRecord, payload.work_item_id)
    if work_item is None:
        return None, "unknown-work-item"
    if work_item.status not in (WorkItemStatus.PLANNED.value, WorkItemStatus.ACTIVE.value):
        return None, "work-item-not-open"

    open_assignment = (
        await db.scalars(
            select(AssignmentRecord).where(
                AssignmentRecord.agent_id == payload.agent_id,
                AssignmentRecord.status == "ACTIVE",
            )
        )
    ).first()
    if open_assignment is not None:
        return None, "agent-has-active-assignment"

    record = AssignmentRecord(
        assignment_id=_new_id(),
        agent_id=payload.agent_id,
        work_item_id=payload.work_item_id,
        status="ACTIVE",
        assigned_by=assigned_by,
        assigned_at=_now(),
        closed_at=None,
    )
    db.add(record)
    # Activating an assignment activates the work item (PLANNED -> ACTIVE).
    if work_item.status == WorkItemStatus.PLANNED.value:
        work_item.status = WorkItemStatus.ACTIVE.value
        work_item.updated_at = _now()
    await db.commit()
    await db.refresh(record)
    return record, None


async def close_assignment(
    db: AsyncSession, assignment_id: str, new_status: str
) -> AssignmentRecord | None:
    """Close an assignment: COMPLETED | RELEASED | SUSPENDED."""
    if new_status not in {"COMPLETED", "RELEASED", "SUSPENDED"}:
        raise ValueError(f"invalid assignment close status: {new_status}")
    record = await db.get(AssignmentRecord, assignment_id)
    if record is None or record.status != "ACTIVE":
        return None
    record.status = new_status
    record.closed_at = _now()
    await db.commit()
    await db.refresh(record)
    return record


async def list_assignments(
    db: AsyncSession, agent_id: str | None = None, status: str | None = None
) -> list[AssignmentRecord]:
    stmt = select(AssignmentRecord).order_by(AssignmentRecord.assigned_at)
    if agent_id is not None:
        stmt = stmt.where(AssignmentRecord.agent_id == agent_id)
    if status is not None:
        stmt = stmt.where(AssignmentRecord.status == status)
    result = await db.scalars(stmt)
    return list(result.all())


async def active_assignment_for_agent(db: AsyncSession, agent_id: str) -> AssignmentRecord | None:
    return (
        await db.scalars(
            select(AssignmentRecord).where(
                AssignmentRecord.agent_id == agent_id,
                AssignmentRecord.status == "ACTIVE",
            )
        )
    ).first()


# ---------------------------------------------------------------- execution tasks


async def create_execution_task(
    db: AsyncSession, payload: ExecutionTaskCreate
) -> tuple[ExecutionTaskRecord | None, str | None]:
    """Link an A2A execution task to a work item (ACMS-REQ-036)."""
    work_item = await db.get(WorkItemRecord, payload.work_item_id)
    if work_item is None:
        return None, "unknown-work-item"
    if payload.agent_id is not None:
        agent = await db.get(AgentRecord, payload.agent_id)
        if agent is None:
            return None, "unknown-agent"
    record = ExecutionTaskRecord(
        task_id=_new_id(),
        work_item_id=payload.work_item_id,
        agent_id=payload.agent_id,
        external_task_id=payload.external_task_id,
        status="RUNNING",
        started_at=_now(),
        finished_at=None,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record, None


async def finish_execution_task(
    db: AsyncSession, task_id: str, new_status: str
) -> ExecutionTaskRecord | None:
    if new_status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return None
    record = await db.get(ExecutionTaskRecord, task_id)
    if record is None or record.status != "RUNNING":
        return None
    record.status = new_status
    record.finished_at = _now()
    await db.commit()
    await db.refresh(record)
    return record


async def list_execution_tasks(
    db: AsyncSession, work_item_id: str | None = None
) -> list[ExecutionTaskRecord]:
    stmt = select(ExecutionTaskRecord).order_by(ExecutionTaskRecord.started_at)
    if work_item_id is not None:
        stmt = stmt.where(ExecutionTaskRecord.work_item_id == work_item_id)
    result = await db.scalars(stmt)
    return list(result.all())


# ---------------------------------------------------------------- routines


async def upsert_routine(
    db: AsyncSession, agent_id: str, payload: RoutineUpsert
) -> tuple[BackgroundRoutineRecord | None, str | None]:
    """Inventory upsert keyed on (agent_id, name). ACMS never schedules these."""
    agent = await db.get(AgentRecord, agent_id)
    if agent is None:
        return None, "unknown-agent"

    record = (
        await db.scalars(
            select(BackgroundRoutineRecord).where(
                BackgroundRoutineRecord.agent_id == agent_id,
                BackgroundRoutineRecord.name == payload.name,
            )
        )
    ).first()
    now = _now()
    if record is None:
        record = BackgroundRoutineRecord(
            routine_id=_new_id(),
            agent_id=agent_id,
            name=payload.name,
            created_at=now,
        )
        db.add(record)
    record.purpose = payload.purpose
    record.schedule = payload.schedule
    record.enabled = payload.enabled
    if payload.latest_status is not None:
        record.latest_status = payload.latest_status
        record.latest_report_at = now
    await db.commit()
    await db.refresh(record)
    return record, None


async def list_routines(db: AsyncSession, agent_id: str | None = None) -> list[BackgroundRoutineRecord]:
    stmt = select(BackgroundRoutineRecord).order_by(BackgroundRoutineRecord.created_at)
    if agent_id is not None:
        stmt = stmt.where(BackgroundRoutineRecord.agent_id == agent_id)
    result = await db.scalars(stmt)
    return list(result.all())


# ---------------------------------------------------------------- handoffs


async def create_handoff(db: AsyncSession, payload: HandoffCreate) -> HandoffRecord | None:
    work_item = await db.get(WorkItemRecord, payload.work_item_id)
    if work_item is None:
        return None
    record = HandoffRecord(
        handoff_id=_new_id(),
        work_item_id=payload.work_item_id,
        agent_id=payload.agent_id,
        title=payload.title,
        body_markdown=payload.body_markdown,
        created_at=_now(),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def list_handoffs(db: AsyncSession, work_item_id: str | None = None) -> list[HandoffRecord]:
    stmt = select(HandoffRecord).order_by(HandoffRecord.created_at)
    if work_item_id is not None:
        stmt = stmt.where(HandoffRecord.work_item_id == work_item_id)
    result = await db.scalars(stmt)
    return list(result.all())
