"""ADR-0012 completion callback API — versioned, scoped-token, fail-closed.

Auth model: this endpoint uses a DEDICATED callback token
(``ACMS_CALLBACK_TOKEN``) rather than the admin token — a worker runtime only
needs the ability to report its own terminal results, not admin authority.
Unset token ⇒ 503 fail-closed (endpoint effectively disabled, matching the
session-secret pattern).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .completion_callback import (
    CompletionCallbackError,
    CompletionCallbackRequest,
    process_completion_callback,
)
from .db import get_session
from .settings import get_settings

router = APIRouter(prefix="/api/v1/callbacks", dependencies=[])


def _require_callback_token(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().callback_token
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="ACMS_CALLBACK_TOKEN not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    if authorization.removeprefix("Bearer ") != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid callback token")


class CompletionCallbackResponse(dict):
    pass


@router.post("/execution-completion", dependencies=[Depends(_require_callback_token)])
async def execution_completion(
    payload: CompletionCallbackRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Terminal-result delivery from the worker runtime (ADR-0012 primary path).

    - 200 ``result=closed`` → task + session atomically closed, semantic event
      emitted (EXECUTION_COMPLETED / EXECUTION_FAILED / EXECUTION_CANCELLED).
    - 200 ``result=already_terminal`` → idempotent replay (duplicate/late
      callback) — harmless by design.
    - 404 unknown-task-id / 403 agent-mismatch → durable
      EXECUTION_CALLBACK_REJECTED audit event.
    """
    try:
        result = await process_completion_callback(db, payload)
    except CompletionCallbackError as e:
        raise HTTPException(status_code=e.http_status,
                            detail={"code": e.code, **e.detail}) from None
    return result