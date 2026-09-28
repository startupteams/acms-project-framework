"""Attention UI — operator view over the derived Attention feed (REV2 Phase D).

Read-only view backed by attention_items() (the same derivation the JSON API
and SSE use). No mutable duplicate Attention state is created here;
acknowledgement/resolution, if required later, becomes an auditable action
layered on the durable event source (plan §6-D1).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..attention_api import attention_items
from ..db import get_session
from .session_auth import current_user

router = APIRouter(prefix="/ui/attention")

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def attention_page(
    request: Request,
    severity: str | None = Query(default=None),
    hours: int = Query(default=72, ge=1, le=24 * 30),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    items = await attention_items(db, hours=hours, severity=severity)
    items.sort(key=lambda i: (_SEV_ORDER.get(i["severity"], 9),
                              -(i["sequence"] or 0)))
    from .routes import templates

    return templates.TemplateResponse(
        request,
        "attention.html",
        {"user": user, "items": items, "severity": severity, "hours": hours},
    )
