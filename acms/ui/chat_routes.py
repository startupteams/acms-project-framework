"""Agent chat UI (STEA-004 Phase C §20-§24).

Behaves like Telegram/ChatGPT against the agent's harness session:
- one normal input box; harness slash commands pass THROUGH the bridge
  (ACMS does not build a custom steer/interactive panel — §21)
- slash autocomplete from harness-advertised commands (§21)
- durable human chat view over the harness transcript: human messages,
  agent responses, tool events as collapsible cards (§22/§23)
- reasoning/hidden chain-of-thought NEVER rendered (§22; bridge sanitizes)
- files appear as downloadable cards when the transcript carries them (§24)

Honesty: when the bridge is unreachable or the harness reports no session,
the page says exactly that — no fabricated conversation.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..bridge import BridgeError, get_bridge_for_agent
from ..db import get_session
from ..registry import list_agents
from .routes import _base_context, templates
from .session_auth import current_user

router = APIRouter(prefix="/ui/agents", include_in_schema=False)


def _templates():
    return templates


def _agent_or_404_sync(agents: list, agent_id: str):
    for a in agents:
        if a.agent_id == agent_id:
            return a
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")


def _tool_summary(content: str) -> str:
    """Short one-line summary of a tool payload for the collapsed default (§23)."""
    try:
        d = json.loads(content) if isinstance(content, str) else (content or {})
    except (TypeError, ValueError):
        d = {}
    if isinstance(d, dict):
        out = d.get("output") or d.get("stdout") or d.get("result") or ""
        err = d.get("error")
        preview = " ".join(str(out).split())[:110]
        if err:
            return f"error: {(' '.join(str(err).split()))[:80]}"
        return preview or "completed"
    return " ".join(str(d).split())[:110] or "completed"


def _safe_args(content: str) -> dict:
    """Arguments if safe (§23): only tiny scalar args, redacting anything that
    looks like a secret. Oversized/complex payloads stay collapsed."""
    try:
        d = json.loads(content) if isinstance(content, str) else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    safe: dict = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 120:
            lk = k.lower()
            if any(s in lk for s in ("key", "token", "secret", "password", "auth")):
                safe[k] = "«redacted»"
            else:
                safe[k] = v
    return safe


def _render_rows(messages: list[dict]) -> list[dict]:
    """Group transcript rows into renderable cards: human/assistant bubbles,
    tool calls as collapsible tool cards. Session dividers come from timestamps
    (day change → divider). No reasoning fields ever reach the template."""
    rows: list[dict] = []
    last_day = None
    for m in messages:
        role = m.get("role") or "unknown"
        ts = m.get("timestamp") or ""
        day = str(ts)[:10]
        if day and day != last_day:
            rows.append({"kind": "divider", "label": day})
            last_day = day
        content = m.get("content") or ""
        if role == "tool":
            rows.append({
                "kind": "tool", "tool_name": m.get("tool_name") or "tool",
                "summary": _tool_summary(content), "safe_args": _safe_args(content),
                "raw": content[:2000], "ts": ts,
            })
        elif role == "user":
            rows.append({"kind": "human", "text": content, "ts": ts})
        else:
            text = content
            if not text and m.get("tool_name"):
                continue
            rows.append({"kind": "agent", "text": text or "(no visible output)", "ts": ts})
    return rows


@router.get("/{agent_id}/chat")
async def agent_chat_page(
    request: Request,
    agent_id: str,
    session_id: str | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    agents = await list_agents(db)
    agent = _agent_or_404_sync(agents, agent_id)
    error = request.query_params.get("error")
    notice = request.query_params.get("notice")

    harness_sessions: list[dict] = []
    rows: list[dict] = []
    commands: list[dict] = []
    bridge_error: str | None = None
    effective_session = session_id

    try:
        bridge = get_bridge_for_agent(agent_id)
        if effective_session is None:
            effective_session = bridge.current_session_id()
        sess_rows = bridge.list_sessions()
        sess_rows.sort(key=lambda s: s.get("last_active") or 0, reverse=True)
        harness_sessions = [{
            "id": str(s.get("id")), "title": s.get("title") or "(untitled)",
            "model": s.get("model"), "message_count": s.get("message_count"),
            "last_active": (str(s.get("last_active") or "")[:16]),
        } for s in sess_rows[:12]]
        if effective_session:
            rows = _render_rows(bridge.fetch_messages(effective_session, limit=200))
        commands = bridge.slash_commands()
    except BridgeError as e:
        bridge_error = f"bridge unreachable: {str(e)[:160]}"
    except Exception as e:  # noqa: BLE001 — page must render with the failure shown
        bridge_error = f"chat source unavailable: {e.__class__.__name__}"

    context = _base_context(user) | {
        "agent": {
            "agent_id": agent.agent_id,
            "display_name": agent.display_name,
            "worker_uid": agent.worker_uid,
            "harness": agent.harness,
        },
        "rows": rows,
        "harness_sessions": harness_sessions,
        "effective_session": effective_session,
        "commands": commands,
        "bridge_error": bridge_error,
        "error": error,
        "notice": notice,
    }
    return _templates().TemplateResponse(request, "agent_chat.html", context)


@router.post("/{agent_id}/chat/send")
async def agent_chat_send(
    request: Request,
    agent_id: str,
    message: str = Form(...),
    session_id: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Send one message (normal text or slash command) THROUGH the bridge (§21).
    Session-bound when the harness has an active session; otherwise a new run."""
    agents = await list_agents(db)
    _agent_or_404_sync(agents, agent_id)
    text = (message or "").strip()
    if not text:
        return RedirectResponse(f"/ui/agents/{agent_id}/chat?error=empty+message",
                                status_code=303)
    sid = session_id or None
    try:
        bridge = get_bridge_for_agent(agent_id)
        if sid is None:
            sid = bridge.current_session_id()
        out = bridge.chat(sid, text)
    except BridgeError as e:
        return RedirectResponse(
            f"/ui/agents/{agent_id}/chat?error={str(e)[:160]}".replace(" ", "+"),
            status_code=303)
    new_sid = sid
    if not sid and isinstance(out, dict):
        # run submission path: session_id may appear as session_id or the run id
        new_sid = out.get("session_id") or out.get("run_id") or sid
    dest = f"/ui/agents/{agent_id}/chat"
    if new_sid:
        dest += f"?session_id={new_sid}"
    dest += "&notice=message+sent"
    return RedirectResponse(dest, status_code=303)


@router.get("/{agent_id}/chat/commands")
async def agent_chat_commands(
    agent_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Harness-advertised slash commands for autocomplete (§21)."""
    agents = await list_agents(db)
    _agent_or_404_sync(agents, agent_id)
    try:
        bridge = get_bridge_for_agent(agent_id)
        return {"commands": bridge.slash_commands()}
    except BridgeError as e:
        return {"commands": [], "error": str(e)[:160]}


@router.get("/{agent_id}/chat/messages")
async def agent_chat_messages(
    agent_id: str,
    session_id: str | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """JSON transcript rows for the chat page's JS refresh (§22). Sanitized:
    no reasoning fields ever leave this route."""
    agents = await list_agents(db)
    _agent_or_404_sync(agents, agent_id)
    try:
        bridge = get_bridge_for_agent(agent_id)
        sid = session_id or bridge.current_session_id()
        if not sid:
            return {"session_id": None, "rows": []}
        return {"session_id": sid, "rows": _render_rows(
            bridge.fetch_messages(sid, limit=200))}
    except BridgeError as e:
        return {"session_id": session_id, "rows": [], "error": str(e)[:160]}