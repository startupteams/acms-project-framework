"""Per-Work budget API (ACMS-REQ-059; REV2 plan §4.4).

Administrator: set/update budget, override hard limit (audited), view.
Worker/Observer: read-only views, consistent with existing role policy.
All budget-math responses distinguish actual spend from UNKNOWN.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from . import budget_service
from .db import get_session
from .memory_models import WorkBudgetRecord
from .security import require_admin_token

# API plane stays bearer-token-gated like the other v1 routers; the
# budget-mutating routes additionally require the admin token.
router = APIRouter(prefix="/api/v1/budgets", dependencies=[Depends(require_admin_token)])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class BudgetSet(BaseModel):
    soft_budget_usd: float | None = Field(default=None, ge=0)
    hard_budget_usd: float | None = Field(default=None, ge=0)


class BudgetOverride(BaseModel):
    override_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=4, max_length=512)


class BudgetView(BaseModel):
    work_item_id: str
    soft_budget_usd: float | None
    hard_budget_usd: float | None
    budget_state: str
    override_active: bool
    override_by: str | None
    override_reason: str | None
    overridden_at: str | None
    updated_at: str | None


class RollupView(BaseModel):
    work_item_id: str
    session_count: int
    open_sessions: int
    cumulative_api_tokens: int
    estimated_cost_usd: float
    estimated_cloud_cost_usd: float
    sessions_with_local_usage: int
    local_cost_usd: float | None
    sessions_unknown_cost: int
    sessions_partial_cost: int
    model_breakdown: dict | None
    budget_state: str


class ExecutionCheck(BaseModel):
    work_item_id: str
    allowed: bool
    reason: str
    budget_state: str | None
    rollup: RollupView | None
    override: dict | None = None


def _budget_view(b: WorkBudgetRecord) -> BudgetView:
    return BudgetView(
        work_item_id=b.work_item_id,
        soft_budget_usd=b.soft_budget_usd,
        hard_budget_usd=b.hard_budget_usd,
        budget_state=b.budget_state,
        override_active=bool(b.override_active),
        override_by=b.override_by,
        override_reason=b.override_reason,
        overridden_at=_iso(b.overridden_at),
        updated_at=_iso(b.updated_at),
    )


@router.put("/work/{work_item_id}", response_model=BudgetView)
async def set_work_budget(work_item_id: str, body: BudgetSet,
                          db: AsyncSession = Depends(get_session)):
    """Set/update soft+hard budget. Must not exceed hard when both set."""
    if (body.soft_budget_usd is not None and body.hard_budget_usd is not None
            and body.soft_budget_usd > body.hard_budget_usd):
        raise HTTPException(status_code=422, detail="soft_budget_usd cannot exceed hard_budget_usd")
    try:
        budget, _created = await budget_service.set_budget(
            db, work_item_id, soft_budget_usd=body.soft_budget_usd,
            hard_budget_usd=body.hard_budget_usd)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _budget_view(budget)


@router.get("/work/{work_item_id}", response_model=BudgetView)
async def get_work_budget(work_item_id: str, db: AsyncSession = Depends(get_session)):
    budget = await budget_service.get_budget(db, work_item_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not set for work item")
    return _budget_view(budget)


@router.get("/work/{work_item_id}/rollup", response_model=RollupView)
async def get_work_rollup(work_item_id: str, db: AsyncSession = Depends(get_session)):
    rollup = await budget_service.work_budget_rollup(db, work_item_id)
    return RollupView(**rollup.to_dict())


@router.post("/work/{work_item_id}/override", response_model=BudgetView)
async def override_work_budget(work_item_id: str, body: BudgetOverride,
                               db: AsyncSession = Depends(get_session)):
    """Audited one-shot hard-limit override."""
    try:
        budget = await budget_service.override_hard_limit(
            db, work_item_id, override_by=body.override_by, reason=body.reason)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _budget_view(budget)


@router.post("/work/{work_item_id}/refresh", response_model=BudgetView)
async def refresh_work_budget(work_item_id: str, db: AsyncSession = Depends(get_session)):
    """Re-evaluate budget_state from current session telemetry (soft/hard events)."""
    budget = await budget_service.refresh_budget_state(db, work_item_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not set for work item")
    return _budget_view(budget)


@router.get("/work/{work_item_id}/execution-check", response_model=ExecutionCheck)
async def execution_check(work_item_id: str, db: AsyncSession = Depends(get_session)):
    """Gate consulted before NEW cloud model execution on this Work."""
    verdict = await budget_service.check_new_execution_allowed(db, work_item_id)
    return ExecutionCheck(work_item_id=work_item_id, **{
        k: v for k, v in verdict.items() if k != "work_item_id"})
