"""Product detail UI (STEA-004 Phase C §14/§16).

Product = durable business/customer offering. Page shows: identity, business
description, status, Projects, repositories (+ link/unlink for admins),
Jira mappings, work counts, cost/usage, Slop Ratio cards + history.

Human corrections: repositories can be associated/disassociated clearly
(§14 — humans correct ACMS-inferred associations). All mutations are
Administrator-only. Slop data comes from slop_metrics (§16 v1: no thresholds).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..a2a_models import (
    ArtifactRecord,
    ProductRecord,
    ProjectRecord,
)
from ..db import get_session
from ..economics_models import CostAttributionRecord
from ..memory_models import ExecutionSessionRecord
from ..slop_metrics import (
    ProductRepositoryLink,
    RepositoryRecord,
    get_or_create_repository,
    product_slop_history,
    product_slop_summary,
    refresh_repository_metrics,
)
from ..work_models import WorkItemRecord
from .roles import Role
from .routes import _base_context, templates
from .session_auth import current_user
from .work_routes import RedirectWithError, _require_admin

router = APIRouter(prefix="/ui/products", include_in_schema=False)


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


async def _product_usage(db: AsyncSession, product_id: str) -> dict:
    """Cost/usage rollup across this product's projects + work (§14)."""
    project_ids = [r.project_id for r in (await db.scalars(
        select(ProjectRecord.project_id)
        .where(ProjectRecord.product_id == product_id))).all()]
    work_q = select(WorkItemRecord.work_item_id)
    if project_ids:
        work_q = work_q.where(WorkItemRecord.project_id.in_(project_ids))
        rows = (await db.scalars(work_q)).all()
    else:
        rows = []
    work_ids = [r[0] if isinstance(r, tuple) else r for r in rows]
    sess_count = tok = 0
    cost = 0.0
    if work_ids:
        sess_count = (await db.scalar(
            select(func.count()).select_from(ExecutionSessionRecord)
            .where(ExecutionSessionRecord.work_item_id.in_(work_ids)))) or 0
        tok = (await db.scalar(
            select(func.sum(func.coalesce(ExecutionSessionRecord.cumulative_api_tokens, 0)))
            .where(ExecutionSessionRecord.work_item_id.in_(work_ids)))) or 0
    # cost attribution rows carry work linkage indirectly via session; keep honest counts only
    return {"sessions": sess_count, "tokens": int(tok or 0), "cost_known": False,
            "note": "product-level cloud cost attribution requires session→product "
                    "rollups (FW); token counts are real"}


@router.get("/{product_id}")
async def product_detail(
    request: Request,
    product_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    p = await db.get(ProductRecord, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="product not found")

    projects = (await db.scalars(
        select(ProjectRecord).where(ProjectRecord.product_id == product_id)
        .order_by(ProjectRecord.name))).all()
    project_ids = [pr.project_id for pr in projects]

    # work counts per project
    work_counts: dict[str, int] = {}
    open_counts: dict[str, int] = {}
    if project_ids:
        for r in (await db.execute(
                select(WorkItemRecord.project_id, func.count())
                .where(WorkItemRecord.project_id.in_(project_ids))
                .group_by(WorkItemRecord.project_id))).all():
            work_counts[r[0]] = r[1]
        for r in (await db.execute(
                select(WorkItemRecord.project_id, func.count())
                .where(WorkItemRecord.project_id.in_(project_ids))
                .where(WorkItemRecord.status.in_(("planned", "active", "in_review", "blocked")))
                .group_by(WorkItemRecord.project_id))).all():
            open_counts[r[0]] = r[1]

    # repositories (direct product links + project links, unique-source §16)
    linked = {}
    for l in (await db.scalars(
            select(ProductRepositoryLink)
            .where(ProductRepositoryLink.product_id == product_id))).all():
        linked[l.repository_id] = "product"
    proj_repo_rows = []
    if project_ids:
        from ..slop_metrics import ProjectRepositoryLink

        proj_repo_rows = (await db.scalars(
            select(ProjectRepositoryLink)
            .where(ProjectRepositoryLink.project_id.in_(project_ids)))).all()
    proj_repo_map = {}
    for l in proj_repo_rows:
        linked.setdefault(l.repository_id, "project")
        proj_repo_map.setdefault(l.repository_id, []).append(l.project_id)
    repos = []
    if linked:
        repo_rows = (await db.scalars(
            select(RepositoryRecord)
            .where(RepositoryRecord.repository_id.in_(list(linked))))).all()
        for r in repo_rows:
            from ..slop_metrics import RepositoryMetricSnapshot

            snaps = (await db.scalar(
                select(func.count()).select_from(RepositoryMetricSnapshot)
                .where(RepositoryMetricSnapshot.repository_id == r.repository_id))) or 0
            repos.append({
                "repository_id": r.repository_id,
                "slug": r.slug,
                "url": r.canonical_url,
                "branch": r.default_branch,
                "loc": r.last_loc,
                "total_adr": r.last_total_adr_count,
                "human_adr": r.last_human_adr_count,
                "measured_str": _fmt(r.last_measured_at),
                "linked_via": linked.get(r.repository_id),
                "snapshots": int(snaps or 0),
            })

    slop = await product_slop_summary(db, product_id)
    history = await product_slop_history(db, product_id)

    # artifacts for this product
    artifacts = (await db.scalars(
        select(ArtifactRecord)
        .where(ArtifactRecord.product_id == product_id)
        .order_by(ArtifactRecord.created_at.desc()).limit(10))).all()

    usage = await _product_usage(db, product_id)

    all_repos = (await db.scalars(select(RepositoryRecord).order_by(RepositoryRecord.owner))).all()

    context = _base_context(user) | {
        "product": {
            "product_id": p.product_id, "name": p.name, "slug": p.slug,
            "business_summary": p.business_summary, "status": p.status,
            "context_notes": p.context_notes,
            "created_str": _fmt(p.created_at),
        },
        "projects": [{
            "project_id": pr.project_id, "name": pr.name, "slug": pr.slug,
            "jira_project_key": pr.jira_project_key,
            "work_count": work_counts.get(pr.project_id, 0),
            "open_count": open_counts.get(pr.project_id, 0),
        } for pr in projects],
        "repos": repos,
        "slop": slop,
        "history": history[-20:],
        "artifacts": [{
            "artifact_uid": a.artifact_uid or a.artifact_id,
            "title": a.title, "type": a.artifact_type,
            "created_str": _fmt(a.created_at),
        } for a in artifacts],
        "usage": usage,
        "all_repos": [{"repository_id": r.repository_id, "slug": r.slug}
                      for r in all_repos if str(r.repository_id) not in linked],
        "is_admin": user.role == Role.ADMINISTRATOR.value,
        "error": request.query_params.get("error"),
    }
    return templates.TemplateResponse(request, "product_detail.html", context)


@router.post("/{product_id}/repositories/link")
async def product_link_repo(
    product_id: str,
    owner: str = Form(""),
    repo: str = Form(""),
    repository_id: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    p = await db.get(ProductRecord, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="product not found")
    try:
        if repository_id:
            rid = repository_id
        else:
            if not owner.strip() or not repo.strip():
                return RedirectWithError(f"/ui/products/{product_id}", "owner/repo required")
            rec = await get_or_create_repository(db, owner.strip(), repo.strip())
            rid = rec.repository_id
        existing = (await db.scalars(
            select(ProductRepositoryLink)
            .where(ProductRepositoryLink.product_id == product_id,
                   ProductRepositoryLink.repository_id == rid))).first()
        if existing is None:
            from datetime import datetime, timezone

            db.add(ProductRepositoryLink(product_id=product_id, repository_id=rid,
                                         created_at=datetime.now(timezone.utc)))
            await db.commit()
    except Exception as e:  # noqa: BLE001
        return RedirectWithError(f"/ui/products/{product_id}", f"link failed: {e.__class__.__name__}")
    return RedirectResponse(f"/ui/products/{product_id}", status_code=303)


@router.post("/{product_id}/repositories/{repository_id}/unlink")
async def product_unlink_repo(
    product_id: str, repository_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    for l in (await db.scalars(
            select(ProductRepositoryLink)
            .where(ProductRepositoryLink.product_id == product_id,
                   ProductRepositoryLink.repository_id == repository_id))).all():
        await db.delete(l)
    await db.commit()
    return RedirectResponse(f"/ui/products/{product_id}", status_code=303)


@router.post("/{product_id}/repositories/{repository_id}/refresh")
async def product_refresh_repo(
    product_id: str, repository_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Refresh-now for one repo (§16 'Refresh now'). Administrator-only."""
    _require_admin(user)
    out = await refresh_repository_metrics(db, repository_id)
    err = next((r.get("error") for r in out if r.get("error")), None)
    dest = f"/ui/products/{product_id}"
    if err:
        return RedirectWithError(dest, f"measurement error: {err[:140]}")
    return RedirectResponse(dest, status_code=303)