"""Product bootstrap API (window-5 plan §8) + migration 0011 tables.

- POST /api/v1/bootstrap/parse        — preview: grounded metadata + classification (no writes)
- GET  /api/v1/bootstrap/ideas        — browse Business Idea Generator (token permitting)
- POST /api/v1/bootstrap/requests     — create a resumable bootstrap request
- POST /api/v1/bootstrap/requests/{id}/execute — run/resume (repo→docs→hierarchy→jira)
- GET  /api/v1/bootstrap/requests/{id} — durable per-step results
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .bootstrap_models import BootstrapRequestRecord
from .db import get_session
from .product_bootstrap import (BootstrapError, fetch_idea_from_github,
                                list_github_ideas, parse_idea_markdown,
                                run_bootstrap, validation_classified,
                                create_bootstrap_request)
from .security import require_admin_token
from .telemetry_service import add_event

router = APIRouter(prefix="/api/v1/bootstrap",
                   dependencies=[Depends(require_admin_token)])


class ParseRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=200000)


class GithubImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=300)


class BootstrapCreate(BaseModel):
    product_name: str = Field(min_length=1, max_length=120)
    markdown: str | None = Field(default=None, max_length=200000)
    github_path: str | None = Field(default=None, max_length=300)
    executive_agent_id: str | None = Field(default=None, max_length=36)
    jira_issue_key: str | None = Field(default=None, max_length=64)
    create_jira: bool = False
    actor: str | None = Field(default=None, max_length=128)


@router.post("/parse")
async def parse(body: ParseRequest):
    """Preview grounded metadata + classification — NO writes, no fabrication."""
    idea = parse_idea_markdown(body.markdown)
    return {"idea": idea, "classification": validation_classified(idea)}


@router.get("/ideas")
async def ideas():
    try:
        return {"ideas": list_github_ideas()}
    except BootstrapError as e:
        return {"ideas": [], "error": {"code": e.code, "detail": str(e)},
                "note": "paste/import the Markdown directly while repo read access is pending (human gate)"}


@router.post("/requests")
async def create_request(body: BootstrapCreate,
                         db: AsyncSession = Depends(get_session)):
    idea: dict
    if body.github_path:
        try:
            idea = fetch_idea_from_github(body.github_path)
        except BootstrapError as e:
            raise HTTPException(status_code=502, detail={"code": e.code, "detail": str(e)}) from None
    elif body.markdown:
        idea = parse_idea_markdown(body.markdown)
    else:
        raise HTTPException(status_code=422, detail={"code": "no-source",
                                                     "detail": "provide markdown or github_path"})
    name = body.product_name or idea.get("title") or "Untitled product"
    req_id = await create_bootstrap_request(db, requested_by=body.actor or "api",
                                            source_kind="github_idea" if body.github_path else "pasted_markdown")
    # stash idea + name durably in steps_json under 'input' before execute
    from .product_bootstrap import BootstrapRequestRecord as R

    req = await db.get(BootstrapRequestRecord, req_id)
    if req is None:  # pragma: no cover — created above in the same tx
        raise HTTPException(status_code=500, detail="bootstrap request vanished")
    req.product_name = name
    req.idea_content_sha256 = idea.get("content_sha256")
    req.steps_json = json.dumps({"input": {"idea": idea, "product_name": name}})
    await db.commit()
    return {"request_id": req_id, "product_name": name,
            "classification": validation_classified(idea),
            "preview": {"idea": idea, "note": "review before execute; nothing created yet"}}


@router.post("/requests/{req_id}/execute")
async def execute(req_id: str, body: dict | None = None,
                  db: AsyncSession = Depends(get_session)):
    req = await db.get(BootstrapRequestRecord, req_id)
    if req is None:
        raise HTTPException(status_code=404, detail="bootstrap request not found")
    steps = json.loads(req.steps_json or "{}")
    idea = (steps.get("input") or {}).get("idea")
    if not idea:
        raise HTTPException(status_code=409, detail={"code": "no-input",
                                                     "detail": "request has no stored idea input"})
    exec_body = body or {}
    try:
        result = await run_bootstrap(
            db, req_id=req_id, product_name=req.product_name or "Untitled product",
            idea=idea, executive_agent_id=exec_body.get("executive_agent_id"),
            jira_issue_key=exec_body.get("jira_issue_key"),
            create_jira=bool(exec_body.get("create_jira", False)),
            actor=exec_body.get("actor") or "api")
    except BootstrapError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "detail": str(e)}) from None
    return result


@router.get("/requests/{req_id}")
async def get_request(req_id: str, db: AsyncSession = Depends(get_session)):
    req = await db.get(BootstrapRequestRecord, req_id)
    if req is None:
        raise HTTPException(status_code=404, detail="bootstrap request not found")
    return {"request_id": req.request_id, "state": req.state,
            "product_name": req.product_name, "steps": json.loads(req.steps_json or "{}"),
            "created_at": req.created_at.isoformat() if req.created_at else None}