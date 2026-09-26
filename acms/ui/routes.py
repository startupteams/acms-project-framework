"""Server-rendered internal web UI routes (ACMS-REQ-023, ACMS-REQ-046 partial; ADR-0008).

Pages are thin read-only views over data the backend actually maintains today.
Per the deployment plan (§5): no fabricated assignment/progress/status/cost
values — those fields do not exist yet and are shown as explicit "not yet
implemented" notices on the dashboard instead (ACMS-REQ-046 stays partial).
"""
from __future__ import annotations

import logging
import time
from importlib import resources
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..db import engine, get_session
from ..registry import list_agents
from ..settings import get_settings
from . import ldap_auth
from .roles import Role
from .session_auth import (
    clear_session_cookie,
    current_user,
    issue_token,
    session_configured,
    set_session_cookie,
)

logger = logging.getLogger("acms.ui")

_APP_STARTED_AT = time.time()

templates_dir = Path(str(resources.files("acms.ui") / "templates"))
static_dir = Path(str(resources.files("acms.ui") / "static"))
templates = Jinja2Templates(directory=str(templates_dir))

router = APIRouter(prefix="/ui", include_in_schema=False)


def install_ui(app) -> None:
    """Attach the UI router and static assets to the FastAPI app."""
    app.mount("/ui/static", StaticFiles(directory=str(static_dir)), name="ui-static")
    app.include_router(router)


def _deny_unmapped(request: Request, username: str) -> Response:
    """Authenticated against LDAP but no mapped ACMS role → deny (plan §6)."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Account is not authorized for ACMS access.",
            "login_disabled": False,
        },
        status_code=status.HTTP_403_FORBIDDEN,
    )


@router.get("/login")
async def login_page(request: Request):
    if not session_configured():
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None, "login_disabled": True},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return templates.TemplateResponse(request, "login.html", {"error": None, "login_disabled": False})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not session_configured():
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Web login is not configured (missing session secret).", "login_disabled": True},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        result = await ldap_auth.authenticate(username, password)
    except ldap_auth.LdapUnavailable:
        logger.warning("LDAP unavailable during login attempt for user %r", username)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Authentication service unavailable. Try again shortly.", "login_disabled": False},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if result.role is None:
        # Generic failure: unknown user, wrong password, or unmapped account.
        return _deny_unmapped(request, username)

    settings = get_settings()
    token = issue_token(result.username, result.role.value)
    response = RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token, secure=settings.session_cookie_secure)
    return response


@router.get("/logout")
async def logout(request: Request):
    settings = get_settings()
    response = RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response, secure=settings.session_cookie_secure)
    return response


async def _database_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — health display, never crash the page
        logger.exception("Database health check failed")
        return False


async def _alembic_revision() -> str | None:
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
            return str(row[0]) if row else None
    except Exception:  # noqa: BLE001 — schema not migrated yet; page must still render
        return None


def _capability_summary(capabilities) -> list[str]:
    flags = [
        name.replace("_", " ")
        for name in ("streaming", "pause", "resume", "interrupt", "cancel", "steering", "transcript_export", "background_routine_inventory")
        if getattr(capabilities, name, False)
    ]
    return flags if flags else ["none declared"]


async def _load_agents(db: AsyncSession):
    agents = await list_agents(db)
    return [
        {
            "agent_id": a.agent_id,
            "display_name": a.display_name,
            "trust_class": a.trust_class.value,
            "harness": a.harness,
            "bridge_version": a.bridge_version,
            "protocol_version": a.protocol_version,
            "capabilities": _capability_summary(a.capabilities),
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }
        for a in agents
    ]


def _base_context(user) -> dict:
    return {"user": user, "version": __version__}


@router.get("/")
async def ui_home(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    agents = await _load_agents(db)
    internal = sum(1 for a in agents if a["trust_class"] == "internal")
    external = sum(1 for a in agents if a["trust_class"] == "external")
    context = _base_context(user) | {
        "app_health": "ok",
        "db_health": "ok" if await _database_ok() else "unreachable",
        "agent_count": len(agents),
        "internal_count": internal,
        "external_count": external,
        # ACMS-REQ-046 partial: these backend fields do not exist yet — never fabricate.
        "not_implemented": ["primary assignment", "agent status", "last contact", "cost"],
    }
    return templates.TemplateResponse(request, "home.html", context)


@router.get("/agents")
async def ui_agents(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    agents = await _load_agents(db)
    context = _base_context(user) | {"agents": agents}
    return templates.TemplateResponse(request, "agents.html", context)


@router.get("/system")
async def ui_system(
    request: Request,
    user=Depends(current_user),
):
    settings = get_settings()
    context = _base_context(user) | {
        "environment": settings.environment,
        "db_health": "ok" if await _database_ok() else "unreachable",
        "alembic_revision": await _alembic_revision(),
        "uptime_seconds": int(time.time() - _APP_STARTED_AT),
        "ldap_configured": bool(settings.ldap_url),
        "session_configured": session_configured(),
    }
    return templates.TemplateResponse(request, "system.html", context)
