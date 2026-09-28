"""A2A dispatch API — REV2 Phase C (budget-gated work delivery).

The single machine endpoint for starting NEW cloud execution of a Work Item.
Budget gating, idempotency, and correlation live in dispatch_service — this
router is a thin adapter. Bearer-gated like the rest of the machine API.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .dispatch_service import DispatchError, dispatch_work
from .security import require_admin_token

router = APIRouter(prefix="/api/v1/dispatch", dependencies=[Depends(require_admin_token)])


class DispatchRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=20000)
    agent_id: str | None = Field(default=None, max_length=36)
    idempotency_key: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=255)
    actor: str | None = Field(default=None, max_length=128)


class DispatchResponse(BaseModel):
    dispatched: bool
    reason: str
    work_item_id: str
    task_id: str | None = None
    external_task_id: str | None = None
    budget_check: dict | None = None


@router.post("/work/{work_item_id}", response_model=DispatchResponse)
async def dispatch_work_item(work_item_id: str, body: DispatchRequest,
                             request: Request,
                             db: AsyncSession = Depends(get_session)) -> DispatchResponse:
    """Dispatch NEW cloud execution for a Work Item (REQ-059 gated).

    - 200 with dispatched=false + reason=hard_budget_exceeded → gate held
      (durable EXECUTION_REJECTED event emitted; visible on /attention).
    - 200 with dispatched=false + reason=duplicate → idempotent replay of an
      in-flight dispatch (original task_id returned).
    - 4xx/5xx DispatchError codes: unknown-work-item (404),
      no-active-assignment (409), agent-mismatch (409), no-bridge-target (503),
      bridge-error (502).
    """
    actor = body.actor or getattr(request.state, "actor", None) or "api"
    try:
        result = await dispatch_work(
            db, work_item_id=work_item_id, instruction=body.instruction,
            agent_id=body.agent_id, idempotency_key=body.idempotency_key,
            session_id=body.session_id, actor=actor,
        )
    except DispatchError as e:
        raise HTTPException(status_code=e.http_status,
                            detail={"code": e.code, **e.detail}) from None
    return DispatchResponse(work_item_id=work_item_id, **result)
