"""A2A production-path API (STEA-004 plan 2026-10-01 §11/§18/§23/§25).

One router family covering:
- /api/v1/projects, /api/v1/products (+ tag attach/detach)
- /api/v1/model-policy (get/set per scope; effective resolution for a work item)
- /api/v1/inbox (list/read/unread/archive/steer/plan/context-markdown)
- /api/v1/artifacts (create/list/get/download)
- /api/v1/execution/{task_id}/stream (SSE replay of persisted execution events)
- /api/v1/execution/{task_id}/events (agent-side ingestion, callback-token gated)

Bearer-gated like every machine API (REQ-039).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .a2a_models import (
    ArtifactRecord,
    ExecutionEventRecord,
    HumanInboxItemRecord,
    ProductRecord,
    ProjectRecord,
    TagLinkRecord,
    TagRecord,
)
from .db import get_session
from .security import require_admin_token
from .uid_keys import allocate_artifact_uid

router = APIRouter(dependencies=[Depends(require_admin_token)])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or f"item-{int(_now().timestamp())}"


# ---------------------------------------------------------------- projects/products


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    product_id: str | None = Field(default=None, max_length=36)
    repos: str | None = None
    jira_project_key: str | None = Field(default=None, max_length=32)
    context_notes: str | None = None
    tags: list[str] = []


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    slug: str
    product_id: str | None
    repos: str | None
    jira_project_key: str | None
    context_notes: str | None
    created_at: datetime
    updated_at: datetime


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    business_summary: str | None = None
    context_notes: str | None = None
    tags: list[str] = []


class ProductResponse(BaseModel):
    product_id: str
    name: str
    slug: str
    business_summary: str | None
    status: str
    context_notes: str | None
    created_at: datetime
    updated_at: datetime


def _project_resp(r: ProjectRecord) -> ProjectResponse:
    return ProjectResponse(project_id=r.project_id, name=r.name, slug=r.slug,
                           product_id=r.product_id, repos=r.repos,
                           jira_project_key=r.jira_project_key,
                           context_notes=r.context_notes,
                           created_at=r.created_at, updated_at=r.updated_at)


def _product_resp(r: ProductRecord) -> ProductResponse:
    return ProductResponse(product_id=r.product_id, name=r.name, slug=r.slug,
                           business_summary=r.business_summary, status=r.status,
                           context_notes=r.context_notes,
                           created_at=r.created_at, updated_at=r.updated_at)


async def _attach_tags(db: AsyncSession, scope: str, scope_id: str, names: list[str]) -> None:
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        tag = (await db.scalars(select(TagRecord).where(TagRecord.name == name))).first()
        if tag is None:
            tag = TagRecord(tag_id=TagRecord.new_id(), name=name, created_at=_now())
            db.add(tag)
            await db.flush()
        exists = (await db.scalars(select(TagLinkRecord).where(
            TagLinkRecord.tag_id == tag.tag_id, TagLinkRecord.scope == scope,
            TagLinkRecord.scope_id == scope_id))).first()
        if exists is None:
            db.add(TagLinkRecord(link_id=TagLinkRecord.new_id(), tag_id=tag.tag_id,
                                 scope=scope, scope_id=scope_id, created_at=_now()))


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_session)):
    if body.product_id is not None:
        if await db.get(ProductRecord, body.product_id) is None:
            raise HTTPException(404, "unknown product")
    rec = ProjectRecord(project_id=ProjectRecord.new_id(), name=body.name,
                        slug=_slug(body.name), product_id=body.product_id,
                        repos=body.repos, jira_project_key=body.jira_project_key,
                        context_notes=body.context_notes,
                        created_at=_now(), updated_at=_now())
    db.add(rec)
    await db.flush()
    await _attach_tags(db, "project", rec.project_id, body.tags)
    await db.commit()
    await db.refresh(rec)
    return _project_resp(rec)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(ProjectRecord).order_by(ProjectRecord.name))).all()
    return [_project_resp(r) for r in rows]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(ProjectRecord, project_id)
    if rec is None:
        raise HTTPException(404, "unknown project")
    return _project_resp(rec)


@router.post("/products", response_model=ProductResponse, status_code=201)
async def create_product(body: ProductCreate, db: AsyncSession = Depends(get_session)):
    rec = ProductRecord(product_id=ProductRecord.new_id(), name=body.name,
                        slug=_slug(body.name), business_summary=body.business_summary,
                        context_notes=body.context_notes,
                        created_at=_now(), updated_at=_now())
    db.add(rec)
    await db.flush()
    await _attach_tags(db, "product", rec.product_id, body.tags)
    await db.commit()
    await db.refresh(rec)
    return _product_resp(rec)


@router.get("/products", response_model=list[ProductResponse])
async def list_products(db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(ProductRecord).order_by(ProductRecord.name))).all()
    return [_product_resp(r) for r in rows]


@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(product_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(ProductRecord, product_id)
    if rec is None:
        raise HTTPException(404, "unknown product")
    return _product_resp(rec)


# ---------------------------------------------------------------- repositories + slop (STEA-004 §15/§16)


class RepositoryCreate(BaseModel):
    owner: str = Field(min_length=1, max_length=128)
    repo: str = Field(min_length=1, max_length=128)
    canonical_url: str | None = Field(default=None, max_length=512)


class RepositoryResponse(BaseModel):
    repository_id: str
    owner: str
    repo: str
    canonical_url: str | None
    default_branch: str | None
    last_commit_sha: str | None
    last_measured_at: datetime | None
    last_loc: int | None
    last_total_adr_count: int | None
    last_human_adr_count: int | None


class RepositoryLinkRequest(BaseModel):
    repository_id: str = Field(max_length=36)


def _repo_resp(r) -> RepositoryResponse:
    return RepositoryResponse(
        repository_id=r.repository_id, owner=r.owner, repo=r.repo,
        canonical_url=r.canonical_url, default_branch=r.default_branch,
        last_commit_sha=r.last_commit_sha, last_measured_at=r.last_measured_at,
        last_loc=r.last_loc, last_total_adr_count=r.last_total_adr_count,
        last_human_adr_count=r.last_human_adr_count)


@router.post("/repositories", response_model=RepositoryResponse, status_code=201)
async def create_repository(body: RepositoryCreate, db: AsyncSession = Depends(get_session)):
    from .slop_metrics import RepositoryRecord, get_or_create_repository

    rec = await get_or_create_repository(db, body.owner, body.repo, body.canonical_url)
    await db.commit()
    await db.refresh(rec)
    return _repo_resp(rec)


@router.get("/repositories", response_model=list[RepositoryResponse])
async def list_repositories(db: AsyncSession = Depends(get_session)):
    from .slop_metrics import RepositoryRecord

    rows = (await db.scalars(select(RepositoryRecord).order_by(RepositoryRecord.owner, RepositoryRecord.repo))).all()
    return [_repo_resp(r) for r in rows]


@router.post("/repositories/{repository_id}/refresh", response_model=list[dict])
async def refresh_one_repository(repository_id: str, db: AsyncSession = Depends(get_session)):
    from .slop_metrics import refresh_repository_metrics

    out = await refresh_repository_metrics(db, repository_id)
    if not out:
        raise HTTPException(404, "unknown repository")
    return out


@router.post("/repository-metrics/refresh", response_model=list[dict])
async def refresh_all_repository_metrics(db: AsyncSession = Depends(get_session)):
    """§16 Refresh-now (manual trigger; hourly scheduler is future wiring)."""
    from .slop_metrics import refresh_repository_metrics

    return await refresh_repository_metrics(db)


@router.post("/products/{product_id}/repositories", status_code=201)
async def link_product_repository(product_id: str, body: RepositoryLinkRequest,
                                  db: AsyncSession = Depends(get_session)):
    from .slop_metrics import ProductRepositoryLink, RepositoryRecord

    if await db.get(ProductRecord, product_id) is None:
        raise HTTPException(404, "unknown product")
    if await db.get(RepositoryRecord, body.repository_id) is None:
        raise HTTPException(404, "unknown repository")
    exists = (await db.scalars(
        select(ProductRepositoryLink)
        .where(ProductRepositoryLink.product_id == product_id,
               ProductRepositoryLink.repository_id == body.repository_id))).first()
    if exists is None:
        db.add(ProductRepositoryLink(product_id=product_id, repository_id=body.repository_id))
        await db.commit()
    return {"linked": True}


@router.delete("/products/{product_id}/repositories/{repository_id}", status_code=204)
async def unlink_product_repository(product_id: str, repository_id: str,
                                    db: AsyncSession = Depends(get_session)):
    from .slop_metrics import ProductRepositoryLink
    from sqlalchemy import delete as _delete

    await db.execute(_delete(ProductRepositoryLink).where(
        ProductRepositoryLink.product_id == product_id,
        ProductRepositoryLink.repository_id == repository_id))
    await db.commit()
    return None


@router.post("/projects/{project_id}/repositories", status_code=201)
async def link_project_repository(project_id: str, body: RepositoryLinkRequest,
                                  db: AsyncSession = Depends(get_session)):
    from .a2a_models import ProjectRecord
    from .slop_metrics import ProjectRepositoryLink, RepositoryRecord

    if await db.get(ProjectRecord, project_id) is None:
        raise HTTPException(404, "unknown project")
    if await db.get(RepositoryRecord, body.repository_id) is None:
        raise HTTPException(404, "unknown repository")
    exists = (await db.scalars(
        select(ProjectRepositoryLink)
        .where(ProjectRepositoryLink.project_id == project_id,
               ProjectRepositoryLink.repository_id == body.repository_id))).first()
    if exists is None:
        db.add(ProjectRepositoryLink(project_id=project_id, repository_id=body.repository_id))
        await db.commit()
    return {"linked": True}


@router.get("/products/{product_id}/slop", response_model=dict)
async def product_slop(product_id: str, db: AsyncSession = Depends(get_session)):
    from .slop_metrics import product_slop_summary

    out = await product_slop_summary(db, product_id)
    if not out:
        raise HTTPException(404, "unknown product")
    return out


@router.get("/products/{product_id}/slop-history", response_model=list[dict])
async def product_slop_history_route(product_id: str, db: AsyncSession = Depends(get_session)):
    from .slop_metrics import product_slop_history

    return await product_slop_history(db, product_id)


# ---------------------------------------------------------------- model policy


class ModelPolicyUpsert(BaseModel):
    scope: str = Field(pattern="^(system|agent|project|work_item)$")
    scope_id: str | None = Field(default=None, max_length=36)
    inference_policy: str = Field(pattern="^(local-only|local-preferred|any)$")
    preferred_model: str | None = Field(default=None, max_length=128)
    cloud_fallback: bool = False
    updated_by: str | None = Field(default=None, max_length=128)


class ModelPolicyResponse(BaseModel):
    scope: str
    scope_id: str | None
    inference_policy: str
    preferred_model: str | None
    cloud_fallback: bool
    updated_at: datetime | None = None


@router.get("/model-policy/effective/{work_item_id}")
async def effective_policy(work_item_id: str, db: AsyncSession = Depends(get_session)):
    from .model_policy import resolve_policy_for_work_item
    from .work_models import WorkItemRecord

    if await db.get(WorkItemRecord, work_item_id) is None:
        raise HTTPException(404, "unknown work item")
    return await resolve_policy_for_work_item(db, work_item_id)


@router.get("/model-policy/{scope}/{scope_id}", response_model=ModelPolicyResponse)
async def get_model_policy(scope: str, scope_id: str, db: AsyncSession = Depends(get_session)):
    from .model_policy import get_policy

    rec = await get_policy(db, scope, None if scope == "system" else scope_id)
    if rec is None:
        raise HTTPException(404, "no policy for scope")
    return ModelPolicyResponse(scope=rec.scope, scope_id=rec.scope_id,
                               inference_policy=rec.inference_policy,
                               preferred_model=rec.preferred_model,
                               cloud_fallback=rec.cloud_fallback,
                               updated_at=rec.updated_at)


@router.put("/model-policy", response_model=ModelPolicyResponse)
async def put_model_policy(body: ModelPolicyUpsert, db: AsyncSession = Depends(get_session)):
    from .model_policy import upsert_policy

    try:
        rec = await upsert_policy(db, scope=body.scope,
                                  scope_id=None if body.scope == "system" else body.scope_id,
                                  inference_policy=body.inference_policy,
                                  preferred_model=body.preferred_model,
                                  cloud_fallback=body.cloud_fallback,
                                  updated_by=body.updated_by)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return ModelPolicyResponse(scope=rec.scope, scope_id=rec.scope_id,
                               inference_policy=rec.inference_policy,
                               preferred_model=rec.preferred_model,
                               cloud_fallback=rec.cloud_fallback,
                               updated_at=rec.updated_at)


# ---------------------------------------------------------------- inbox


class InboxItemResponse(BaseModel):
    item_id: str
    item_class: str
    severity: str
    title: str
    summary: str | None
    agent_id: str | None
    work_item_id: str | None
    execution_session_id: str | None
    project_id: str | None
    product_id: str | None
    jira_issue_key: str | None
    read: bool
    archived: bool
    created_at: datetime
    updated_at: datetime
    metadata: dict = {}


def _inbox_resp(r: HumanInboxItemRecord) -> InboxItemResponse:
    try:
        meta = json.loads(r.metadata_json) if r.metadata_json else {}
    except (TypeError, ValueError):
        meta = {}
    return InboxItemResponse(
        item_id=r.item_id, item_class=r.item_class, severity=r.severity,
        title=r.title, summary=r.summary, agent_id=r.agent_id,
        work_item_id=r.work_item_id, execution_session_id=r.execution_session_id,
        project_id=r.project_id, product_id=r.product_id,
        jira_issue_key=r.jira_issue_key, read=r.read_at is not None,
        archived=r.archived_at is not None, created_at=r.created_at,
        updated_at=r.updated_at, metadata=meta)


class InboxAction(BaseModel):
    action: str = Field(pattern="^(steer|incorporate|reply|archive)$")
    message: str | None = Field(default=None, max_length=20000)
    force: bool = False
    actor: str | None = Field(default=None, max_length=128)


@router.get("/inbox", response_model=list[InboxItemResponse])
async def list_inbox_items(
    unread_only: bool = False, item_class: str | None = None,
    severity: str | None = None, agent_id: str | None = None,
    project_id: str | None = None, include_archived: bool = False,
    search: str | None = None, limit: int = Query(default=100, le=300),
    db: AsyncSession = Depends(get_session),
):
    from .inbox_service import list_inbox

    rows = await list_inbox(db, unread_only=unread_only, item_class=item_class,
                            severity=severity, agent_id=agent_id,
                            project_id=project_id, include_archived=include_archived,
                            search=search, limit=limit)
    return [_inbox_resp(r) for r in rows]


@router.get("/inbox/{item_id}", response_model=InboxItemResponse)
async def get_inbox_item(item_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        raise HTTPException(404, "unknown inbox item")
    return _inbox_resp(rec)


@router.post("/inbox/{item_id}/read", response_model=InboxItemResponse)
async def mark_inbox_read(item_id: str, read: bool = True, db: AsyncSession = Depends(get_session)):
    from .inbox_service import mark_read

    rec = await mark_read(db, item_id, read=read)
    if rec is None:
        raise HTTPException(404, "unknown inbox item")
    return _inbox_resp(rec)


@router.post("/inbox/{item_id}/action", response_model=InboxItemResponse)
async def inbox_action(item_id: str, body: InboxAction, db: AsyncSession = Depends(get_session)):
    """Human reply actions (plan §24): steer active process / incorporate into
    planning / reply-only / archive."""
    from .bridge import BridgeError, get_bridge_for_agent
    from .inbox_service import archive_item
    from .work_models import WorkItemRecord

    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        raise HTTPException(404, "unknown inbox item")

    if body.action == "archive":
        rec2, err = await archive_item(db, item_id, force=body.force)
        if err == "unresolved-action-required":
            raise HTTPException(409, "unresolved ACTION_REQUIRED requires force=true")
        if rec2 is None:
            raise HTTPException(404, "unknown inbox item")
        return _inbox_resp(rec2)

    if body.action == "steer":
        if not rec.work_item_id or not body.message:
            raise HTTPException(422, "steer requires work_item link + message")
        if not rec.agent_id:
            raise HTTPException(409, "no agent bound; steer unavailable")
        # find the live harness session for the agent
        from .telemetry_models import AgentSessionBindingRecord

        bind = await db.get(AgentSessionBindingRecord, rec.agent_id)
        session_id = bind.session_id if bind else None
        if not session_id:
            raise HTTPException(409, "no live session bound; steer unavailable")
        try:
            bridge = get_bridge_for_agent(rec.agent_id)
            bridge.steer(session_id, body.message)
        except BridgeError as e:
            raise HTTPException(502, f"steer failed: {str(e)[:200]}") from None
        rec.metadata_json = json.dumps({**_meta(rec), "steered": True,
                                        "steer_message": body.message[:2000],
                                        "steered_by": body.actor or "api",
                                        "steered_at": _now().isoformat()})
        rec.updated_at = _now()
        await db.commit()
        await db.refresh(rec)
        return _inbox_resp(rec)

    if body.action == "incorporate":
        if not body.message:
            raise HTTPException(422, "incorporate requires message")
        if rec.work_item_id:
            work = await db.get(WorkItemRecord, rec.work_item_id)
            if work is not None:
                work.scope_markdown = (work.scope_markdown or "") + (
                    f"\n\n## Human planning input ({_now().date()} by {body.actor or 'human'})\n\n"
                    f"{body.message}\n")
                work.updated_at = _now()
        rec.metadata_json = json.dumps({**_meta(rec), "incorporated": True,
                                        "message": body.message[:2000],
                                        "by": body.actor or "human"})
        rec.read_at = rec.read_at or _now()
        rec.updated_at = _now()
        await db.commit()
        await db.refresh(rec)
        return _inbox_resp(rec)

    # reply-only: record the reply, mark read
    rec.metadata_json = json.dumps({**_meta(rec), "replies": _meta(rec).get("replies", []) + [
        {"message": body.message, "by": body.actor or "human", "at": _now().isoformat()}]})
    rec.read_at = rec.read_at or _now()
    rec.updated_at = _now()
    await db.commit()
    await db.refresh(rec)
    return _inbox_resp(rec)


def _meta(rec: HumanInboxItemRecord) -> dict:
    try:
        data = json.loads(rec.metadata_json) if rec.metadata_json else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


@router.get("/inbox/{item_id}/context-markdown")
async def inbox_context_markdown(item_id: str, db: AsyncSession = Depends(get_session)):
    """Generate Context Markdown on demand (plan §24) and store as artifact."""
    from .a2a_models import ArtifactRecord
    from .work_models import WorkItemRecord

    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        raise HTTPException(404, "unknown inbox item")
    parts = [f"# Context — {rec.title}", ""]
    parts.append(f"Class: {rec.item_class}  Severity: {rec.severity}")
    parts.append(f"Created: {rec.created_at.isoformat()}")
    if rec.summary:
        parts += ["", f"**Summary:** {rec.summary}"]
    if rec.work_item_id:
        work = await db.get(WorkItemRecord, rec.work_item_id)
        if work is not None:
            parts += ["", "## Work Item", f"- {work.work_key or work.work_item_id}: {work.title}",
                      f"- Status: {work.status}",
                      f"- Jira: {work.jira_issue_key or '(none)'}"]
            if work.scope_markdown:
                parts += ["", "### Approved scope (excerpt)",
                          work.scope_markdown[:3000]]
    if rec.jira_issue_key:
        parts += ["", f"## Jira", f"[{rec.jira_issue_key}](https://jira/startupteams/browse/{rec.jira_issue_key})"]
    meta = _meta(rec)
    if meta:
        parts += ["", "## Event trail", "```json", json.dumps(meta, indent=1, default=str)[:3000], "```"]
    parts += ["", "## Decision points", "- (human completes this section)"]
    content = "\n".join(parts)
    _seq, _uid = await allocate_artifact_uid(db, _now())
    art = ArtifactRecord(
        artifact_id=ArtifactRecord.new_id(),
        artifact_uid=_uid, artifact_sequence=_seq,
        work_item_id=rec.work_item_id, agent_id=rec.agent_id,
        project_id=rec.project_id, product_id=rec.product_id,
        jira_issue_key=rec.jira_issue_key, artifact_type="context",
        title=f"Context: {rec.title[:200]}", content=content,
        sha256=ArtifactRecord.sha(content), created_at=_now(), created_by="inbox-context")
    db.add(art)
    rec.read_at = rec.read_at or _now()
    await db.commit()
    await db.refresh(art)
    return {"artifact_id": art.artifact_id, "content": content}


# ---------------------------------------------------------------- artifacts


class ArtifactCreate(BaseModel):
    work_item_id: str | None = Field(default=None, max_length=36)
    execution_session_id: str | None = Field(default=None, max_length=36)
    agent_id: str | None = Field(default=None, max_length=36)
    project_id: str | None = Field(default=None, max_length=36)
    product_id: str | None = Field(default=None, max_length=36)
    jira_issue_key: str | None = Field(default=None, max_length=64)
    artifact_type: str = Field(default="handoff", max_length=32)
    title: str = Field(min_length=1, max_length=255)
    bluf: str | None = Field(default=None, max_length=512)
    content: str = Field(min_length=1)
    created_by: str | None = Field(default=None, max_length=128)


class ArtifactResponse(BaseModel):
    artifact_id: str
    artifact_uid: str | None = None
    work_item_id: str | None
    agent_id: str | None
    project_id: str | None
    product_id: str | None
    jira_issue_key: str | None
    artifact_type: str
    title: str
    bluf: str | None
    mime_type: str
    sha256: str
    created_at: datetime
    created_by: str | None
    size: int


def _artifact_resp(r: ArtifactRecord) -> ArtifactResponse:
    return ArtifactResponse(artifact_id=r.artifact_id, artifact_uid=r.artifact_uid,
                            work_item_id=r.work_item_id,
                            agent_id=r.agent_id, project_id=r.project_id,
                            product_id=r.product_id, jira_issue_key=r.jira_issue_key,
                            artifact_type=r.artifact_type, title=r.title, bluf=r.bluf,
                            mime_type=r.mime_type, sha256=r.sha256,
                            created_at=r.created_at, created_by=r.created_by,
                            size=len(r.content or ""))


@router.post("/artifacts", response_model=ArtifactResponse, status_code=201)
async def create_artifact(body: ArtifactCreate, db: AsyncSession = Depends(get_session)):
    from .work_models import WorkItemRecord

    if body.work_item_id and await db.get(WorkItemRecord, body.work_item_id) is None:
        raise HTTPException(404, "unknown work item")
    _seq, _uid = await allocate_artifact_uid(db, _now())
    rec = ArtifactRecord(
        artifact_id=ArtifactRecord.new_id(),
        artifact_uid=_uid, artifact_sequence=_seq,
        work_item_id=body.work_item_id,
        execution_session_id=body.execution_session_id, agent_id=body.agent_id,
        project_id=body.project_id, product_id=body.product_id,
        jira_issue_key=body.jira_issue_key, artifact_type=body.artifact_type,
        title=body.title, bluf=body.bluf, content=body.content,
        sha256=ArtifactRecord.sha(body.content), created_at=_now(),
        created_by=body.created_by)
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return _artifact_resp(rec)


@router.get("/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(work_item_id: str | None = None, limit: int = Query(default=50, le=200),
                         q: str | None = None,
                         db: AsyncSession = Depends(get_session)):
    stmt = select(ArtifactRecord).order_by(ArtifactRecord.created_at.desc()).limit(limit)
    if work_item_id:
        stmt = stmt.where(ArtifactRecord.work_item_id == work_item_id)
    if q:
        # Multi-key search (plan §8): UID, UUID, short hash, sha256 full/prefix,
        # title, Jira key, Work UID/short key. Structured LIKE match — semantic
        # search is explicitly out of scope for Phase 1 (plan §27).
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            ArtifactRecord.artifact_uid.ilike(like)
            | ArtifactRecord.artifact_id.ilike(like)
            | ArtifactRecord.title.ilike(like)
            | ArtifactRecord.jira_issue_key.ilike(like)
            | ArtifactRecord.sha256.ilike(like)
            | ArtifactRecord.work_item_id.ilike(like)
        )
    rows = (await db.scalars(stmt)).all()
    return [_artifact_resp(r) for r in rows]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(artifact_id: str, db: AsyncSession = Depends(get_session)):
    # Accept internal UUID, full UID, or sha256 prefix (plan §8 compatibility).
    rec = await db.get(ArtifactRecord, artifact_id)
    if rec is None and artifact_id.startswith("ACMS-"):
        rec = (await db.scalars(
            select(ArtifactRecord).where(ArtifactRecord.artifact_uid == artifact_id).limit(1)
        )).first()
    if rec is None and len(artifact_id) >= 8:
        rec = (await db.scalars(
            select(ArtifactRecord).where(ArtifactRecord.sha256.startswith(artifact_id)).limit(1)
        )).first()
    if rec is None:
        raise HTTPException(404, "unknown artifact")
    return _artifact_resp(rec)


@router.get("/artifacts/{artifact_id}/content")
async def artifact_content(artifact_id: str, download: bool = False,
                           db: AsyncSession = Depends(get_session)):
    rec = await db.get(ArtifactRecord, artifact_id)
    if rec is None:
        raise HTTPException(404, "unknown artifact")
    filename = f"{rec.artifact_id}-{rec.title[:40].replace(' ', '_')}.md"
    if download:
        from urllib.parse import quote

        from fastapi.responses import Response

        return Response(content=rec.content, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
    return {"artifact_id": rec.artifact_id, "title": rec.title,
            "sha256": rec.sha256, "content": rec.content}


# ---------------------------------------------------------------- execution events


class ExecutionEventIn(BaseModel):
    """Agent-side event ingestion (artifact/human_attention upstream too)."""
    events: list[dict] = Field(min_length=1, max_length=200)


@router.post("/execution/{task_id}/events", status_code=202)
async def ingest_execution_events(task_id: str, body: ExecutionEventIn,
                                  db: AsyncSession = Depends(get_session)):
    """Persist worker-reported live events (the run_event pump uses this too).
    Event payload is sanitized — never store secrets or full tool args."""
    from .work_models import ExecutionTaskRecord

    task = await db.get(ExecutionTaskRecord, task_id)
    if task is None:
        raise HTTPException(404, "unknown execution task")
    max_seq = (await db.execute(
        select(ExecutionEventRecord.seq).where(ExecutionEventRecord.task_id == task_id)
        .order_by(ExecutionEventRecord.seq.desc()).limit(1))).scalar()
    seq = (max_seq or 0)
    added = 0
    for ev in body.events:
        etype = str(ev.get("event") or ev.get("event_type") or "unknown")[:64]
        seq += 1
        db.add(ExecutionEventRecord(
            task_id=task_id, seq=seq, event_type=etype, occurred_at=_now(),
            payload_json=json.dumps(_sanitize_event(ev))[:8000]))
        added += 1
    await db.commit()
    return {"accepted": added, "last_seq": seq}


def _sanitize_event(ev: dict) -> dict:
    """Allow-list projection (ADR-0014): no secrets, no hidden reasoning, capped previews."""
    out: dict[str, Any] = {}
    for key in ("event", "run_id", "tool", "duration", "error"):
        if ev.get(key) is not None:
            out[key] = ev[key]
    for key, cap in (("preview", 500), ("delta", 500), ("output", 2000)):
        if ev.get(key) is not None:
            out[key] = str(ev[key])[:cap]
    if isinstance(ev.get("usage"), dict):
        out["usage"] = {k: ev["usage"].get(k) for k in ("input_tokens", "output_tokens", "total_tokens")
                        if ev["usage"].get(k) is not None}
    return out


@router.get("/execution/{task_id}/events")
async def list_execution_events(task_id: str, after_seq: int = 0, limit: int = Query(default=200, le=500),
                                db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(
        select(ExecutionEventRecord)
        .where(ExecutionEventRecord.task_id == task_id,
               ExecutionEventRecord.seq > after_seq)
        .order_by(ExecutionEventRecord.seq).limit(limit))).all()
    return [{"seq": r.seq, "event_type": r.event_type,
             "occurred_at": r.occurred_at.isoformat(),
             "payload": json.loads(r.payload_json) if r.payload_json else {}}
            for r in rows]


@router.get("/execution/{task_id}/stream")
async def execution_event_stream(task_id: str, request: Request,
                                 last_id: int = 0):
    """Browser SSE: replays persisted execution events after last_id, then tails.
    Auth rides the same session/bearer as the UI (cookie or bearer accepted)."""
    from .db import SessionLocal

    async def _gen():
        yield ": connected (ACMS execution event stream)\n\n"
        cursor = last_id
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            async with SessionLocal() as db:
                rows = (await db.scalars(
                    select(ExecutionEventRecord)
                    .where(ExecutionEventRecord.task_id == task_id,
                           ExecutionEventRecord.seq > cursor)
                    .order_by(ExecutionEventRecord.seq).limit(100))).all()
            for r in rows:
                cursor = r.seq
                payload = r.payload_json or "{}"
                yield f"id: {r.seq}\nevent: {r.event_type}\ndata: {payload}\n\n"
                idle = 0
            if not rows:
                idle += 1
                if idle > 120:  # ~2 min idle then close; browser reconnects
                    return
                yield ": keepalive\n\n"
            import asyncio

            await asyncio.sleep(1.0)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})