"""Global 'Search anything' UI page (STEA-004 Phase C §27).

Phase 1 = structured/fuzzy search over the SAME backend derivation as
``GET /search`` (search_api.global_search) — grouped results, no vector
infrastructure (plan §27 explicitly defers semantic search).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..search_api import global_search as backend_search
from .routes import _base_context, templates
from .session_auth import current_user

router = APIRouter(prefix="/ui/search", include_in_schema=False)


@router.get("")
async def search_page(
    request: Request,
    q: str = Query(default=""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    results = None
    error = None
    if q.strip() and len(q.strip()) >= 2:
        try:
            results = await backend_search(q=q, db=db)
        except Exception as e:  # noqa: BLE001 — page must render with the error
            error = f"{e.__class__.__name__}: {str(e)[:160]}"
    context = _base_context(user) | {
        "q": q, "results": results, "error": error,
    }
    return templates.TemplateResponse(request, "search.html", context)