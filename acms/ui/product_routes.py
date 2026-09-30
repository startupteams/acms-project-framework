"""Products / Projects UI (window-5 plan §8/§11).

- /ui/products — product + project list (kind=product/project work items)
- /ui/products/new — idea source (paste/upload/github) + idea REVIEW screen
  (§8.4) with editable names/scope; submit creates the resumable bootstrap
  request; execute respects the Jira gate (drafts stay non-executable).
- /ui/projects/{id} — project workspace (§11): Overview/Work/PRs/Requirements/
  Artifacts/Usage tabs over EXISTING ACMS concepts (work_items hierarchy +
  economics pr_outcomes + budget rollups).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..bootstrap_api import ParseRequest, create_request
from ..bootstrap_models import BootstrapRequestRecord
from ..db import get_session
from ..economics_models import PrOutcomeRecord
from ..product_bootstrap import parse_idea_markdown, validation_classified, BootstrapError
from ..work_models import WorkItemRecord
from .roles import Role
from .session_auth import current_user
from .work_routes import RedirectWithError, RedirectSeeOther, _require_admin, _templates

router = APIRouter(prefix="/ui", include_in_schema=False)


@router.get("/products")
async def products_list(request: Request, user=Depends(current_user),
                        db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(
        select(WorkItemRecord)
        .where(WorkItemRecord.kind.in_(("product", "project")))
        .order_by(WorkItemRecord.updated_at.desc())
    )).all()
    context = {"user": user, "items": rows,
               "error": request.query_params.get("error")}
    return _templates().TemplateResponse(request, "products.html", context)


@router.get("/products/new")
async def product_new_form(request: Request, user=Depends(current_user)):
    if user.role != Role.ADMINISTRATOR:
        return RedirectWithError("/ui/products", "Administrator role required")
    context = {"user": user,
               "source_kind": request.query_params.get("source", "paste"),
               "error": request.query_params.get("error"),
               "preview": None}
    return _templates().TemplateResponse(request, "product_new.html", context)


@router.post("/products/preview")
async def product_preview(request: Request,
                          markdown: str = Form("", max_length=200000),
                          product_name: str = Form("", max_length=120),
                          user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
    """§8.4 idea review screen — grounded metadata + classification, no writes."""
    if user.role != Role.ADMINISTRATOR:
        return RedirectWithError("/ui/products", "Administrator role required")
    if not markdown.strip():
        return RedirectWithError("/ui/products/new", "paste or upload the idea Markdown")
    idea = parse_idea_markdown(markdown)
    context = {
        "user": user,
        "preview": {
            "idea": idea,
            "classification": validation_classified(idea),
            "product_name": product_name or idea.get("title") or "Untitled product",
            "markdown": markdown,
        },
        "error": None,
    }
    return _templates().TemplateResponse(request, "product_new.html", context)


@router.post("/products/create")
async def product_create(request: Request,
                         markdown: str = Form("", max_length=200000),
                         product_name: str = Form(..., max_length=120),
                         jira_issue_key: str = Form("", max_length=64),
                         create_jira: bool = Form(False),
                         executive_agent_id: str = Form("", max_length=36),
                         execute: bool = Form(False),
                         user=Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
    """Create the resumable bootstrap request (+optional execute).

    Execution of repo/docs/hierarchy is scaffolding (allowed pre-kickoff per
    §7.1); AI planning/implementation requires the Jira gate.
    """
    if user.role != Role.ADMINISTRATOR:
        return RedirectWithError("/ui/products", "Administrator role required")
    if not markdown.strip():
        return RedirectWithError("/ui/products/new", "idea markdown missing")
    try:
        from ..bootstrap_api import BootstrapCreate

        out = await create_request(
            BootstrapCreate(
                product_name=product_name, markdown=markdown,
                jira_issue_key=jira_issue_key or None,
                create_jira=create_jira,
                executive_agent_id=executive_agent_id or None,
                actor=f"ui:{user.username}"),
            db)
    except BootstrapError as e:
        return RedirectWithError("/ui/products/new", f"{e.code}: {str(e)[:200]}")
    rid = out["request_id"]
    if execute:
        from ..product_bootstrap import run_bootstrap

        req_row = await db.get(BootstrapRequestRecord, rid)
        if req_row is None:
            return RedirectWithError("/ui/products", "bootstrap request vanished")
        steps = json.loads(req_row.steps_json or "{}")
        idea = (steps.get("input") or {}).get("idea")
        if not idea:
            return RedirectWithError("/ui/products", "bootstrap request has no idea input")
        try:
            result = await run_bootstrap(
                db, req_id=rid, product_name=product_name, idea=idea,
                executive_agent_id=executive_agent_id or None,
                jira_issue_key=jira_issue_key or None, create_jira=create_jira,
                actor=f"ui:{user.username}")
        except BootstrapError as e:
            return RedirectWithError(f"/ui/products",
                                     f"bootstrap step failed: {e.code} — {str(e)[:200]} (artifacts retained)")
        wid = (result or {}).get("hierarchy", {}).get("initial_work_item_id")
        return RedirectSeeOther(f"/ui/work/{wid}" if wid else "/ui/products?bootstrap=awaiting")
    return RedirectSeeOther(f"/ui/products?bootstrap_request={rid}")


@router.get("/usage")
async def usage_page(request: Request,
                     work_item_id: str | None = None,
                     agent_id: str | None = None,
                     model_id: str | None = None,
                     since_days: int | None = None,
                     user=Depends(current_user),
                     db: AsyncSession = Depends(get_session)):
    """§12.2/§12.3: honest usage view with Product/Work/Agent/model/date filters."""
    from ..usage_service import usage_summary

    s = await usage_summary(db, work_item_id=work_item_id or None,
                            agent_id=agent_id or None, model_id=model_id or None,
                            since_days=since_days)
    context = {
        "user": user,
        "s": s["sessions"],
        "by_model": s["by_model"],
        "o": s["outcomes"],
        "f": {"work_item_id": work_item_id, "agent_id": agent_id,
              "model_id": model_id, "since_days": since_days},
    }
    return _templates().TemplateResponse(request, "usage.html", context)


@router.get("/projects/{work_item_id}")
async def project_detail(work_item_id: str, request: Request,
                         tab: str = "overview",
                         user=Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
    """§11 project workspace — Project → Work → PR → Handoff without hunting."""
    item = await db.get(WorkItemRecord, work_item_id)
    if item is None:
        from fastapi import HTTPException, status as st

        raise HTTPException(status_code=st.HTTP_404_NOT_FOUND, detail="project not found")
    children = (await db.scalars(
        select(WorkItemRecord)
        .where(WorkItemRecord.parent_id == work_item_id)
        .order_by(WorkItemRecord.updated_at.desc())
    )).all()
    prs = (await db.scalars(
        select(PrOutcomeRecord)
        .where(PrOutcomeRecord.work_item_id == work_item_id)
        .order_by(PrOutcomeRecord.outcome_id.desc())
    )).all()
    context = {
        "user": user, "item": item, "children": children, "prs": prs,
        "tab": tab if tab in ("overview", "work", "prs", "requirements") else "overview",
        "error": request.query_params.get("error"),
    }
    return _templates().TemplateResponse(request, "project_detail.html", context)