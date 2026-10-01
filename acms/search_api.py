"""Power/usage + fleet sort/search + global search APIs (STEA-004 §17/§19/§27/§33).

- GET /api/v1/power/summary        (latest ingested snapshot; ?refresh=1 ingests now)
- GET /api/v1/usage/summary        (cost_attribution rollups, ?hours=)
- GET /api/v1/agents               gains q=, sort=, health=, model= filters (§19)
- GET /api/v1/search?q=            grouped global search (§27 phase 1, structured)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .security import require_admin_token

router = APIRouter(dependencies=[Depends(require_admin_token)])


@router.get("/power/summary")
async def power_summary(refresh: bool = False, db: AsyncSession = Depends(get_session)):
    """§17 facility view. refresh=1 ingests a fresh snapshot from LLM Manager."""
    from .power_ingest import ingest_power_snapshot, latest_power_summary

    if refresh:
        return await ingest_power_snapshot(db)
    out = await latest_power_summary(db)
    if out is None:
        raise HTTPException(404, "no power snapshot yet — call /power/summary?refresh=1")
    return out


@router.get("/usage/summary")
async def usage_summary(hours: int = Query(default=24, le=720), db: AsyncSession = Depends(get_session)):
    from .power_ingest import usage_summary as _usage

    return await _usage(db, hours)


@router.get("/search")
async def global_search(q: str = Query(min_length=2), db: AsyncSession = Depends(get_session)):
    """§27 global 'Search anything' — grouped structured search, phase 1."""
    from .a2a_models import (ArtifactRecord, HumanInboxItemRecord, ProductRecord,
                             ProjectRecord)
    from .models import AgentRecord
    from .work_models import WorkItemRecord

    like = f"%{q.strip()}%"
    ql = q.strip().lower()
    out: dict[str, list] = {"work": [], "artifacts": [], "agents": [],
                            "projects": [], "products": [], "inbox": []}

    works = (await db.scalars(
        select(WorkItemRecord)
        .where((WorkItemRecord.work_uid.ilike(like))
               | (WorkItemRecord.work_key.ilike(like))
               | (WorkItemRecord.title.ilike(like))
               | (WorkItemRecord.work_item_id.ilike(like))
               | (WorkItemRecord.jira_issue_key.ilike(like)))
        .limit(10))).all()
    out["work"] = [{"work_uid": w.work_uid or w.work_key, "title": w.title,
                    "status": w.status, "work_item_id": w.work_item_id} for w in works]

    arts = (await db.scalars(
        select(ArtifactRecord)
        .where((ArtifactRecord.artifact_uid.ilike(like))
               | (ArtifactRecord.artifact_id.ilike(like))
               | (ArtifactRecord.title.ilike(like))
               | (ArtifactRecord.sha256.ilike(like))
               | (ArtifactRecord.jira_issue_key.ilike(like)))
        .limit(10))).all()
    out["artifacts"] = [{"artifact_uid": a.artifact_uid or a.artifact_id, "title": a.title,
                         "artifact_type": a.artifact_type,
                         "artifact_id": a.artifact_id} for a in arts]

    agents = (await db.scalars(
        select(AgentRecord)
        .where((AgentRecord.display_name.ilike(like))
               | (AgentRecord.legacy_name.ilike(like))
               | (AgentRecord.worker_uid.ilike(like))
               | (AgentRecord.agent_id.ilike(like)))
        .limit(10))).all()
    out["agents"] = [{"display_name": a.display_name, "agent_id": a.agent_id,
                      "worker_uid": getattr(a, "worker_uid", None)} for a in agents]

    projects = (await db.scalars(
        select(ProjectRecord)
        .where((ProjectRecord.name.ilike(like)) | (ProjectRecord.slug.ilike(like))
               | (ProjectRecord.jira_project_key.ilike(like)))
        .limit(10))).all()
    out["projects"] = [{"project_id": p.project_id, "name": p.name, "slug": p.slug}
                       for p in projects]

    products = (await db.scalars(
        select(ProductRecord)
        .where((ProductRecord.name.ilike(like)) | (ProductRecord.slug.ilike(like)))
        .limit(10))).all()
    out["products"] = [{"product_id": p.product_id, "name": p.name, "slug": p.slug}
                       for p in products]

    inbox = (await db.scalars(
        select(HumanInboxItemRecord)
        .where((HumanInboxItemRecord.title.ilike(like))
               | (HumanInboxItemRecord.summary.ilike(like)))
        .limit(10))).all()
    out["inbox"] = [{"item_id": i.item_id, "title": i.title,
                     "item_class": i.item_class} for i in inbox]
    return {"q": q, **out}