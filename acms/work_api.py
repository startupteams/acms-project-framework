"""Work orchestration API (ACMS-REQ-007/008/013/014/036).

All endpoints require the machine bearer token (ACMS-REQ-039). Read endpoints
are also consumed by the UI via the service layer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .security import require_admin_token
from .work_models import (
    AssignmentCreate,
    AssignmentResponse,
    ExecutionTaskCreate,
    ExecutionTaskResponse,
    HandoffCreate,
    HandoffResponse,
    RoutineResponse,
    RoutineUpsert,
    WorkItemCreate,
    WorkItemResponse,
    WorkItemUpdate,
)
from . import work_service

router = APIRouter(prefix="/api/v1/work", dependencies=[Depends(require_admin_token)])


def _item_response(record) -> WorkItemResponse:
    return WorkItemResponse(
        work_item_id=record.work_item_id,
        parent_id=record.parent_id,
        kind=record.kind,
        title=record.title,
        created_by=record.created_by,
        status=record.status,
        scope_markdown=record.scope_markdown,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _assignment_response(record) -> AssignmentResponse:
    return AssignmentResponse(
        assignment_id=record.assignment_id,
        agent_id=record.agent_id,
        work_item_id=record.work_item_id,
        status=record.status,
        assigned_by=record.assigned_by,
        assigned_at=record.assigned_at,
        closed_at=record.closed_at,
    )


def _task_response(record) -> ExecutionTaskResponse:
    return ExecutionTaskResponse(
        task_id=record.task_id,
        work_item_id=record.work_item_id,
        agent_id=record.agent_id,
        external_task_id=record.external_task_id,
        status=record.status,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _routine_response(record) -> RoutineResponse:
    return RoutineResponse(
        routine_id=record.routine_id,
        agent_id=record.agent_id,
        name=record.name,
        purpose=record.purpose,
        schedule=record.schedule,
        enabled=record.enabled,
        latest_status=record.latest_status,
        latest_report_at=record.latest_report_at,
        created_at=record.created_at,
    )


def _handoff_response(record) -> HandoffResponse:
    return HandoffResponse(
        handoff_id=record.handoff_id,
        work_item_id=record.work_item_id,
        agent_id=record.agent_id,
        title=record.title,
        body_markdown=record.body_markdown,
        created_at=record.created_at,
    )


_ERROR_TO_STATUS = {
    "unknown-agent": status.HTTP_404_NOT_FOUND,
    "unknown-work-item": status.HTTP_404_NOT_FOUND,
    "work-item-not-open": status.HTTP_409_CONFLICT,
    "agent-has-active-assignment": status.HTTP_409_CONFLICT,
}


# ---------------------------------------------------------------- work items


@router.post("/items", response_model=WorkItemResponse, status_code=status.HTTP_201_CREATED)
async def api_create_work_item(
    payload: WorkItemCreate,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> WorkItemResponse:
    try:
        record = await work_service.create_work_item(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _item_response(record)


@router.get("/items", response_model=list[WorkItemResponse])
async def api_list_work_items(
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> list[WorkItemResponse]:
    return [_item_response(r) for r in await work_service.list_work_items(db)]


@router.get("/items/{work_item_id}", response_model=WorkItemResponse)
async def api_get_work_item(
    work_item_id: str,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> WorkItemResponse:
    record = await work_service.get_work_item(db, work_item_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="work item not found")
    return _item_response(record)


@router.patch("/items/{work_item_id}", response_model=WorkItemResponse)
async def api_update_work_item(
    work_item_id: str,
    payload: WorkItemUpdate,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> WorkItemResponse:
    record = await work_service.update_work_item(db, work_item_id, payload)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="work item not found")
    return _item_response(record)


# ---------------------------------------------------------------- assignments


@router.post("/assignments", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
async def api_assign_primary(
    payload: AssignmentCreate,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> AssignmentResponse:
    record, error = await work_service.assign_primary(db, payload)
    if record is None:
        code = error or "unknown-error"
        raise HTTPException(
            status_code=_ERROR_TO_STATUS.get(code, status.HTTP_400_BAD_REQUEST), detail=code
        )
    return _assignment_response(record)


@router.post("/assignments/{assignment_id}/close", response_model=AssignmentResponse)
async def api_close_assignment(
    assignment_id: str,
    new_status: str,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> AssignmentResponse:
    try:
        record = await work_service.close_assignment(db, assignment_id, new_status)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="active assignment not found")
    return _assignment_response(record)


@router.get("/assignments", response_model=list[AssignmentResponse])
async def api_list_assignments(
    agent_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> list[AssignmentResponse]:
    records = await work_service.list_assignments(db, agent_id=agent_id, status=status)
    return [_assignment_response(r) for r in records]


# ---------------------------------------------------------------- execution tasks


@router.post("/tasks", response_model=ExecutionTaskResponse, status_code=status.HTTP_201_CREATED)
async def api_create_execution_task(
    payload: ExecutionTaskCreate,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> ExecutionTaskResponse:
    record, error = await work_service.create_execution_task(db, payload)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error)
    return _task_response(record)


@router.post("/tasks/{task_id}/finish", response_model=ExecutionTaskResponse)
async def api_finish_execution_task(
    task_id: str,
    new_status: str,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> ExecutionTaskResponse:
    record = await work_service.finish_execution_task(db, task_id, new_status)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found or already finished"
        )
    return _task_response(record)


@router.get("/tasks", response_model=list[ExecutionTaskResponse])
async def api_list_execution_tasks(
    work_item_id: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> list[ExecutionTaskResponse]:
    records = await work_service.list_execution_tasks(db, work_item_id)
    return [_task_response(r) for r in records]


# ---------------------------------------------------------------- routines


@router.put("/agents/{agent_id}/routines", response_model=RoutineResponse)
async def api_upsert_routine(
    agent_id: str,
    payload: RoutineUpsert,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> RoutineResponse:
    record, error = await work_service.upsert_routine(db, agent_id, payload)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error)
    return _routine_response(record)


@router.get("/routines", response_model=list[RoutineResponse])
async def api_list_routines(
    agent_id: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> list[RoutineResponse]:
    records = await work_service.list_routines(db, agent_id)
    return [_routine_response(r) for r in records]


# ---------------------------------------------------------------- handoffs


@router.post("/handoffs", response_model=HandoffResponse, status_code=status.HTTP_201_CREATED)
async def api_create_handoff(
    payload: HandoffCreate,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> HandoffResponse:
    record = await work_service.create_handoff(db, payload)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="work item not found")
    return _handoff_response(record)


@router.get("/handoffs", response_model=list[HandoffResponse])
async def api_list_handoffs(
    work_item_id: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> list[HandoffResponse]:
    records = await work_service.list_handoffs(db, work_item_id)
    return [_handoff_response(r) for r in records]
