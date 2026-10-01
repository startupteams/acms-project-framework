"""A2A production-path server-rendered UI (STEA-004 plan §15/§23/§25/§30/§31).

Pages: /ui/inbox (+ detail/actions), /ui/artifacts (+ viewer), /ui/execution/{task_id}
(live activity). Session-cookie gated like every UI page; actions are
Administrator-only (observers/workers get 303/403 per roles.py).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..a2a_models import (
    ArtifactRecord,
    ExecutionEventRecord,
    HumanInboxItemRecord,
    ProductRecord,
    ProjectRecord,
)
from ..db import get_session
from ..settings import get_settings
from ..work_models import ExecutionTaskRecord, WorkItemRecord
from .roles import Role
from .routes import _base_context, templates
from .session_auth import current_user

router = APIRouter(prefix="/ui", include_in_schema=False)


def _role(user) -> Role:
    try:
        return Role(str(user.role))
    except ValueError:
        return Role.OBSERVER


def _admin_only(user):
    if _role(user) != Role.ADMINISTRATOR:
        return RedirectResponse("/ui/inbox", status_code=303)
    return None


# ---------------------------------------------------------------- inbox


@router.get("/inbox")
async def ui_inbox(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
    item_class: str | None = None,
    unread_only: bool = False,
    include_archived: bool = False,
    search: str | None = None,
):
    from ..inbox_service import list_inbox

    rows = await list_inbox(db, unread_only=unread_only, item_class=item_class,
                            include_archived=include_archived, search=search)
    items = [{
        "item_id": r.item_id, "item_class": r.item_class, "severity": r.severity,
        "title": r.title, "summary": r.summary, "read": r.read_at is not None,
        "work_item_id": r.work_item_id, "jira_issue_key": r.jira_issue_key,
        "created": r.created_at.strftime("%m-%d %H:%M") if r.created_at else "",
        "archived": r.archived_at is not None,
    } for r in rows]
    context = _base_context(user) | {
        "items": items, "item_class": item_class, "unread_only": unread_only,
        "include_archived": include_archived, "search": search or "",
        "counts": {
            "unread": sum(1 for i in items if not i["read"]),
            "action": sum(1 for i in items if i["item_class"] == "ACTION_REQUIRED"),
            "shown": len(items),
        },
    }
    return templates.TemplateResponse(request, "inbox.html", context)


@router.get("/inbox/{item_id}")
async def ui_inbox_detail(
    request: Request, item_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        return RedirectResponse("/ui/inbox", status_code=303)
    import json

    try:
        meta = json.loads(rec.metadata_json) if rec.metadata_json else {}
    except (TypeError, ValueError):
        meta = {}
    work = await db.get(WorkItemRecord, rec.work_item_id) if rec.work_item_id else None
    context = _base_context(user) | {
        "item": {
            "item_id": rec.item_id, "item_class": rec.item_class,
            "severity": rec.severity, "title": rec.title, "summary": rec.summary,
            "read": rec.read_at is not None, "archived": rec.archived_at is not None,
            "work_item_id": rec.work_item_id, "agent_id": rec.agent_id,
            "jira_issue_key": rec.jira_issue_key,
            "created": rec.created_at.strftime("%m-%d %H:%M") if rec.created_at else "",
            "metadata": meta,
        },
        "work": {"title": work.title, "status": work.status,
                 "work_key": work.work_key} if work else None,
    }
    return templates.TemplateResponse(request, "inbox_detail.html", context)


@router.post("/inbox/{item_id}/read")
async def ui_inbox_read(request: Request, item_id: str, user=Depends(current_user),
                        db: AsyncSession = Depends(get_session), read: str = Form("1")):
    from ..inbox_service import mark_read

    gate = _admin_only(user)
    if gate:
        return gate
    await mark_read(db, item_id, read=read == "1")
    return RedirectResponse(f"/ui/inbox/{item_id}", status_code=303)


@router.post("/inbox/{item_id}/action")
async def ui_inbox_action(
    request: Request, item_id: str,
    action: str = Form(...),
    message: str = Form(""),
    force: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    gate = _admin_only(user)
    if gate:
        return gate
    base = f"/ui/inbox/{item_id}"
    if action == "archive":
        from ..inbox_service import archive_item

        _, err = await archive_item(db, item_id, force=force == "1")
        if err == "unresolved-action-required":
            return RedirectResponse(base + "?archive_warn=1", status_code=303)
        return RedirectResponse("/ui/inbox", status_code=303)
    if action in ("steer", "incorporate", "reply") and message:
        from ..bridge import BridgeError, get_bridge_for_agent
        from ..telemetry_models import AgentSessionBindingRecord
        from ..work_models import WorkItemRecord

        rec = await db.get(HumanInboxItemRecord, item_id)
        if rec is None:
            return RedirectResponse("/ui/inbox", status_code=303)
        import json

        try:
            meta = json.loads(rec.metadata_json) if rec.metadata_json else {}
        except (TypeError, ValueError):
            meta = {}
        if action == "steer":
            if rec.agent_id:
                bind = await db.get(AgentSessionBindingRecord, rec.agent_id)
                session_id = bind.session_id if bind else None
                if session_id:
                    try:
                        bridge = get_bridge_for_agent(rec.agent_id)
                        bridge.steer(session_id, message)
                        meta["steered"] = True
                        meta["steer_message"] = message[:2000]
                    except BridgeError:
                        meta["steer_error"] = "bridge unreachable"
                else:
                    meta["steer_error"] = "no live session bound"
        elif action == "incorporate" and rec.work_item_id:
            work = await db.get(WorkItemRecord, rec.work_item_id)
            if work is not None:
                from datetime import datetime, timezone

                today = datetime.now(timezone.utc).date()
                work.scope_markdown = (work.scope_markdown or "") + (
                    f"\n\n## Human planning input ({today} by {user.username})\n\n{message}\n")
                meta["incorporated"] = True
        else:
            meta.setdefault("replies", []).append(
                {"message": message, "by": user.username})
        rec.metadata_json = json.dumps(meta)
        rec.read_at = rec.read_at or HumanInboxItemRecord.now()
        rec.updated_at = HumanInboxItemRecord.now()
        await db.commit()
    return RedirectResponse(base, status_code=303)


@router.post("/inbox/{item_id}/context-markdown")
async def ui_inbox_context(item_id: str, user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
    gate = _admin_only(user)
    if gate:
        return gate
    import json as _json
    from datetime import datetime, timezone

    from ..a2a_api import inbox_context_markdown  # reuse the generator

    class _R:  # minimal shim: call the shared generator logic directly
        pass

    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        return RedirectResponse("/ui/inbox", status_code=303)
    # inline the same generation as the API route (kept in one place there)
    from ..work_models import WorkItemRecord as _W

    parts = [f"# Context — {rec.title}", ""]
    parts.append(f"Class: {rec.item_class}  Severity: {rec.severity}")
    parts.append(f"Created: {rec.created_at.isoformat() if rec.created_at else ''}")
    if rec.summary:
        parts += ["", f"**Summary:** {rec.summary}"]
    if rec.work_item_id:
        work = await db.get(_W, rec.work_item_id)
        if work is not None:
            parts += ["", "## Work Item",
                      f"- {work.work_key or work.work_item_id}: {work.title}",
                      f"- Status: {work.status}",
                      f"- Jira: {work.jira_issue_key or '(none)'}"]
            if work.scope_markdown:
                parts += ["", "### Approved scope (excerpt)", work.scope_markdown[:3000]]
    if rec.jira_issue_key:
        parts += ["", "## Jira", f"[{rec.jira_issue_key}](https://jira/startupteams/browse/{rec.jira_issue_key})"]
    try:
        meta = _json.loads(rec.metadata_json) if rec.metadata_json else {}
    except (TypeError, ValueError):
        meta = {}
    if meta:
        parts += ["", "## Event trail", "```json", _json.dumps(meta, indent=1, default=str)[:3000], "```"]
    parts += ["", "## Decision points", "- (human completes this section)"]
    content = "\n".join(parts)
    art = ArtifactRecord(
        artifact_id=ArtifactRecord.new_id(), work_item_id=rec.work_item_id,
        agent_id=rec.agent_id, project_id=rec.project_id, product_id=rec.product_id,
        jira_issue_key=rec.jira_issue_key, artifact_type="context",
        title=f"Context: {rec.title[:200]}", content=content,
        sha256=ArtifactRecord.sha(content), created_at=ArtifactRecord.now(),
        created_by=f"inbox-context:{user.username}")
    db.add(art)
    await db.commit()
    await db.refresh(art)
    return RedirectResponse(f"/ui/artifacts/{art.artifact_id}", status_code=303)


# ---------------------------------------------------------------- artifacts


@router.get("/artifacts")
async def ui_artifacts(request: Request, user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(
        select(ArtifactRecord).order_by(ArtifactRecord.created_at.desc()).limit(100))).all()
    items = [{
        "artifact_id": r.artifact_id, "title": r.title, "bluf": r.bluf,
        "artifact_type": r.artifact_type, "jira_issue_key": r.jira_issue_key,
        "work_item_id": r.work_item_id,
        "created": r.created_at.strftime("%m-%d %H:%M") if r.created_at else "",
        "size": len(r.content or ""),
    } for r in rows]
    context = _base_context(user) | {"items": items}
    return templates.TemplateResponse(request, "artifacts.html", context)


@router.get("/artifacts/{artifact_id}")
async def ui_artifact_detail(request: Request, artifact_id: str,
                             user=Depends(current_user),
                             db: AsyncSession = Depends(get_session)):
    rec = await db.get(ArtifactRecord, artifact_id)
    if rec is None:
        return RedirectResponse("/ui/artifacts", status_code=303)
    context = _base_context(user) | {
        "a": {"artifact_id": rec.artifact_id, "title": rec.title, "bluf": rec.bluf,
              "content": rec.content, "sha256": rec.sha256,
              "jira_issue_key": rec.jira_issue_key, "work_item_id": rec.work_item_id,
              "created_by": rec.created_by,
              "created": rec.created_at.strftime("%m-%d %H:%M") if rec.created_at else ""},
    }
    return templates.TemplateResponse(request, "artifact_detail.html", context)


@router.get("/artifacts/{artifact_id}/download")
async def ui_artifact_download(artifact_id: str, user=Depends(current_user),
                               db: AsyncSession = Depends(get_session)):
    rec = await db.get(ArtifactRecord, artifact_id)
    if rec is None:
        return RedirectResponse("/ui/artifacts", status_code=303)
    from urllib.parse import quote

    filename = quote(f"{rec.artifact_id[:8]}-{rec.title[:40].replace(' ', '_')}.md")
    return Response(
        content=rec.content, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


# ---------------------------------------------------------------- live execution


@router.get("/projects/new")
async def ui_project_new(request: Request, user=Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
    gate = _admin_only(user)
    if gate:
        return gate
    products = (await db.scalars(select(ProductRecord).order_by(ProductRecord.name))).all()
    context = _base_context(user) | {
        "products": [{"product_id": p.product_id, "name": p.name} for p in products],
        "error": request.query_params.get("error"),
    }
    return templates.TemplateResponse(request, "project_new.html", context)


@router.post("/projects/create")
async def ui_project_create(
    request: Request,
    name: str = Form(...),
    product_id: str = Form(""),
    repos: str = Form(""),
    jira_project_key: str = Form(""),
    context_notes: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    gate = _admin_only(user)
    if gate:
        return gate
    from ..a2a_api import _slug

    rec = ProjectRecord(project_id=ProjectRecord.new_id(), name=name.strip(),
                        slug=_slug(name), product_id=product_id or None,
                        repos=repos or None, jira_project_key=jira_project_key or None,
                        context_notes=context_notes or None,
                        created_at=ProjectRecord.now(), updated_at=ProjectRecord.now())
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return RedirectResponse("/ui/projects", status_code=303)


@router.get("/projects")
async def ui_projects_list(request: Request, user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(ProjectRecord).order_by(ProjectRecord.name))).all()
    products = {p.product_id: p.name for p in (
        await db.scalars(select(ProductRecord))).all()}
    from sqlalchemy import func

    projects = []
    for r in rows:
        work_count = (await db.scalar(
            select(func.count()).select_from(WorkItemRecord)
            .where(WorkItemRecord.project_id == r.project_id))) or 0
        projects.append({
            "project_id": r.project_id, "name": r.name, "slug": r.slug,
            "product": products.get(r.product_id), "repos": r.repos,
            "jira_project_key": r.jira_project_key, "work_count": work_count,
        })
    context = _base_context(user) | {"projects": projects}
    return templates.TemplateResponse(request, "projects.html", context)


@router.get("/execution/{task_id}")
async def ui_execution_live(request: Request, task_id: str,
                            user=Depends(current_user),
                            db: AsyncSession = Depends(get_session)):
    task = await db.get(ExecutionTaskRecord, task_id)
    if task is None:
        return RedirectResponse("/ui/work", status_code=303)
    work = await db.get(WorkItemRecord, task.work_item_id)
    events = (await db.scalars(
        select(ExecutionEventRecord)
        .where(ExecutionEventRecord.task_id == task_id)
        .order_by(ExecutionEventRecord.seq))).all()
    import json as _json

    event_rows = []
    for e in events:
        try:
            payload = _json.loads(e.payload_json) if e.payload_json else {}
        except (TypeError, ValueError):
            payload = {}
        event_rows.append({
            "seq": e.seq, "type": e.event_type,
            "ts": e.occurred_at.strftime("%H:%M:%S") if e.occurred_at else "",
            "tool": payload.get("tool"),
            "preview": payload.get("preview") or payload.get("delta") or payload.get("output") or "",
            "usage": payload.get("usage"),
        })
    context = _base_context(user) | {
        "task": {"task_id": task.task_id, "status": task.status,
                 "transport_state": task.transport_state,
                 "external_task_id": task.external_task_id,
                 "effective_model": task.effective_model,
                 "model_reason": task.model_resolution_reason,
                 "started": task.started_at.strftime("%m-%d %H:%M") if task.started_at else ""},
        "work": {"title": work.title, "work_key": work.work_key,
                 "jira_issue_key": work.jira_issue_key} if work else None,
        "events": event_rows,
        "sse_url": f"/api/v1/execution/{task_id}/stream",
        "last_seq": event_rows[-1]["seq"] if event_rows else 0,
    }
    return templates.TemplateResponse(request, "execution_live.html", context)