"""Engineering-economics API (REV2 plan §9B).

PR-outcome records with explicit acceptance states, many-to-many requirement
links, cost attribution (failed runs included), and the model-comparison
report. Bearer-gated; state transitions to ACCEPTED require accepted_by.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import economics_service
from .db import get_session
from .economics_models import (
    OUTCOME_STATES,
    TASK_CATEGORIES,
    CostAttributionRecord,
    PrOutcomeRecord,
    RequirementLinkRecord,
)
from .security import require_admin_token

router = APIRouter(prefix="/api/v1/economics", dependencies=[Depends(require_admin_token)])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class OutcomeCreate(BaseModel):
    work_item_id: str | None = None
    agent_id: str | None = None
    repository: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    commit_shas: list[str] = Field(default_factory=list)
    model_id: str | None = None
    provider: str | None = None
    task_category: str | None = None
    session_count: int | None = None
    checkpoint_count: int | None = None
    first_pass: bool | None = None
    notes: str | None = None


class OutcomeUpdate(BaseModel):
    outcome_state: str | None = None
    accepted_by: str | None = None
    task_category: str | None = None
    human_review_minutes: float | None = Field(default=None, ge=0)
    human_repair_minutes: float | None = Field(default=None, ge=0)
    first_pass: bool | None = None
    notes: str | None = None


class RequirementLink(BaseModel):
    requirement_refs: list[str] = Field(min_length=1)


class CostEntry(BaseModel):
    session_id: str | None = None
    model_id: str | None = None
    provider: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    api_cost_usd: float | None = Field(default=None, ge=0)
    local_compute_seconds: float | None = Field(default=None, ge=0)
    failed_run: bool = False
    source: str = "manual"


class OutcomeView(BaseModel):
    outcome_id: str
    work_item_id: str | None
    agent_id: str | None
    repository: str | None
    branch: str | None
    pr_number: int | None
    pr_url: str | None
    commit_shas: list[str]
    outcome_state: str
    accepted_at: str | None
    accepted_by: str | None
    model_id: str | None
    provider: str | None
    task_category: str | None
    human_review_minutes: float | None
    human_repair_minutes: float | None
    first_pass: bool | None
    requirement_refs: list[str]


def _view(o: PrOutcomeRecord, refs: list[str]) -> OutcomeView:
    return OutcomeView(
        outcome_id=o.outcome_id, work_item_id=o.work_item_id, agent_id=o.agent_id,
        repository=o.repository, branch=o.branch, pr_number=o.pr_number,
        pr_url=o.pr_url, commit_shas=economics_service.commits_list(o.commit_shas_json),
        outcome_state=o.outcome_state, accepted_at=_iso(o.accepted_at),
        accepted_by=o.accepted_by, model_id=o.model_id, provider=o.provider,
        task_category=o.task_category, human_review_minutes=o.human_review_minutes,
        human_repair_minutes=o.human_repair_minutes, first_pass=o.first_pass,
        requirement_refs=refs,
    )


async def _refs_for(db: AsyncSession, outcome_id: str) -> list[str]:
    rows = (await db.execute(select(RequirementLinkRecord)
                             .where(RequirementLinkRecord.outcome_id == outcome_id))).scalars().all()
    return sorted(r.requirement_ref for r in rows)


@router.post("/outcomes", response_model=OutcomeView, status_code=201)
async def create_outcome(body: OutcomeCreate, db: AsyncSession = Depends(get_session)):
    if body.task_category and body.task_category not in TASK_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"task_category must be one of {TASK_CATEGORIES}")
    rec = PrOutcomeRecord(
        outcome_id=PrOutcomeRecord.new_id(),
        work_item_id=body.work_item_id, agent_id=body.agent_id,
        repository=body.repository, branch=body.branch,
        pr_number=body.pr_number, pr_url=body.pr_url,
        commit_shas_json=economics_service.commits_json(body.commit_shas),
        outcome_state="OPEN", model_id=body.model_id, provider=body.provider,
        task_category=body.task_category, session_count=body.session_count,
        checkpoint_count=body.checkpoint_count, first_pass=body.first_pass,
        notes=body.notes,
    )
    db.add(rec)
    await db.commit()
    return _view(rec, [])


@router.get("/outcomes/{outcome_id}", response_model=OutcomeView)
async def get_outcome(outcome_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(PrOutcomeRecord, outcome_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="outcome not found")
    return _view(rec, await _refs_for(db, outcome_id))


@router.patch("/outcomes/{outcome_id}", response_model=OutcomeView)
async def update_outcome(outcome_id: str, body: OutcomeUpdate,
                         db: AsyncSession = Depends(get_session)):
    rec = await db.get(PrOutcomeRecord, outcome_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="outcome not found")
    try:
        if body.outcome_state is not None:
            await economics_service.set_outcome_state(
                db, rec, new_state=body.outcome_state,
                accepted_by=body.accepted_by)
        elif body.accepted_by is not None:
            raise HTTPException(status_code=422, detail="accepted_by requires outcome_state=ACCEPTED")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if body.task_category is not None:
        if body.task_category not in TASK_CATEGORIES:
            raise HTTPException(status_code=422, detail="invalid task_category")
        rec.task_category = body.task_category
    if body.human_review_minutes is not None:
        rec.human_review_minutes = body.human_review_minutes
    if body.human_repair_minutes is not None:
        rec.human_repair_minutes = body.human_repair_minutes
    if body.first_pass is not None:
        rec.first_pass = body.first_pass
    if body.notes is not None:
        rec.notes = body.notes
    await db.commit()
    return _view(rec, await _refs_for(db, outcome_id))


@router.post("/outcomes/{outcome_id}/requirements", response_model=OutcomeView)
async def link_requirements(outcome_id: str, body: RequirementLink,
                            db: AsyncSession = Depends(get_session)):
    rec = await db.get(PrOutcomeRecord, outcome_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="outcome not found")
    await economics_service.link_requirements(db, outcome_id, body.requirement_refs)
    return _view(rec, await _refs_for(db, outcome_id))


@router.post("/outcomes/{outcome_id}/costs", status_code=201)
async def add_cost_entry(outcome_id: str, body: CostEntry,
                         db: AsyncSession = Depends(get_session)):
    if await db.get(PrOutcomeRecord, outcome_id) is None:
        raise HTTPException(status_code=404, detail="outcome not found")
    if body.session_id:
        try:
            created = await economics_service.attach_session_cost(db, outcome_id, body.session_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return created
    rec = CostAttributionRecord(
        entry_id=CostAttributionRecord.new_id(), outcome_id=outcome_id,
        model_id=body.model_id, provider=body.provider,
        input_tokens=body.input_tokens, output_tokens=body.output_tokens,
        cached_input_tokens=body.cached_input_tokens,
        api_cost_usd=body.api_cost_usd,
        local_compute_seconds=body.local_compute_seconds,
        source=body.source, failed_run=body.failed_run,
    )
    db.add(rec)
    await db.commit()
    return {"entry_id": rec.entry_id, "api_cost_usd": rec.api_cost_usd}


@router.get("/reports/model-comparison")
async def model_comparison(
    db: AsyncSession = Depends(get_session),
    model_id: str | None = None,
    provider: str | None = None,
    repository: str | None = None,
    task_category: str | None = None,
    work_item_id: str | None = None,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    """Canonical metrics for a model/task/repo slice (REV2 §9B.6).

    cost_per_accepted_* is None when nothing is ACCEPTED — unknown, not zero.
    """
    rep = await economics_service.build_report(
        db, model_id=model_id, provider=provider, repository=repository,
        task_category=task_category, work_item_id=work_item_id,
        date_from=date_from, date_to=date_to)
    return rep.to_dict(model_id=model_id, provider=provider, repository=repository,
                       task_category=task_category, work_item_id=work_item_id,
                       date_from=str(date_from) if date_from else None,
                       date_to=str(date_to) if date_to else None)
