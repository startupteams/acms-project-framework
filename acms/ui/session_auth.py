"""Signed server-side session cookies for human web login (plan §6).

Token format: ``base64url(json payload) + "." + base64url(HMAC-SHA256(secret, payload))``.
Payload: ``{"u": username, "r": role, "exp": unix_epoch}``.

- HttpOnly + SameSite=Lax always; ``Secure`` when configured (default on).
- Fail closed: with no/placeholder ``ACMS_SESSION_SECRET`` the UI login is
  disabled entirely rather than operating insecurely.
- The machine bearer token is never used here or exposed to the browser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request, status

from ..settings import get_settings

COOKIE_NAME = "acms_session"

_PLACEHOLDER_SECRETS = {"", "change-me", "replace-with-a-random-secret"}


class SessionUser:
    __slots__ = ("username", "role", "expires_at")

    def __init__(self, username: str, role: str, expires_at: int) -> None:
        self.username = username
        self.role = role
        self.expires_at = expires_at


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(secret: str, payload_b64: str) -> str:
    digest = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64encode(digest)


def session_configured() -> bool:
    """True when a real session signing secret is configured."""
    return get_settings().session_secret not in _PLACEHOLDER_SECRETS


def issue_token(username: str, role: str, lifetime_minutes: int | None = None) -> str:
    settings = get_settings()
    if not session_configured():
        raise RuntimeError("ACMS_SESSION_SECRET is not configured; UI login is disabled")
    minutes = lifetime_minutes if lifetime_minutes is not None else settings.session_lifetime_minutes
    payload = json.dumps(
        {"u": username, "r": role, "exp": int(time.time()) + minutes * 60},
        separators=(",", ":"),
    )
    payload_b64 = _b64encode(payload.encode())
    return f"{payload_b64}.{_sign(settings.session_secret, payload_b64)}"


def verify_token(token: str | None) -> SessionUser | None:
    if not token or "." not in token or not session_configured():
        return None
    payload_b64, sig = token.split(".", 1)
    settings = get_settings()
    if not hmac.compare_digest(sig, _sign(settings.session_secret, payload_b64)):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        expires_at = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if expires_at < time.time():
        return None
    username, role = payload.get("u"), payload.get("r")
    if not isinstance(username, str) or not username or not isinstance(role, str) or not role:
        return None
    return SessionUser(username, role, expires_at)


def current_user(request: Request) -> SessionUser:
    """FastAPI dependency: resolve the session cookie, else redirect to login."""
    user = verify_token(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/ui/login"},
        )
    return user


def set_session_cookie(response, token: str, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=get_settings().session_lifetime_minutes * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response, secure: bool) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
