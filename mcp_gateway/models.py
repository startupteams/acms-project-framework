"""Gateway data models (plan §12/§13/§14/§16/§17/§19/§24)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import uuid

from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RiskClass(StrEnum):
    READ = "READ"
    SAFE_WRITE = "SAFE_WRITE"
    SENSITIVE_WRITE = "SENSITIVE_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


class Role(StrEnum):
    WORKER = "worker"
    REVIEWER = "reviewer"
    PROJECT_MANAGER = "project_manager"
    EXECUTIVE = "executive"
    INFRASTRUCTURE_ADMIN = "infrastructure_admin"
    OBSERVER = "observer"


class TokenKind(StrEnum):
    AGENT = "agent"
    ASSIGNMENT = "assignment"


class AgentTokenRecord(BaseModel):
    """Agent-scoped identity token (plan §12): revocable, narrow, expiring."""

    token_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    token_hash: str  # sha256 hex of the raw token; raw NEVER stored
    agent_name: str
    acms_agent_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)  # e.g. ["acms.read", "acms.write"]
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    # Optional bind: token accepted only from these source IP prefixes (defense-in-depth)
    source_ip_allowlist: list[str] = Field(default_factory=list)

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True


class AssignmentTokenRecord(BaseModel):
    """Assignment-scoped token (plan §13): short-lived, bound to Work/Project/Jira."""

    token_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    token_hash: str
    agent_name: str
    work_uid: str
    project_id: str | None = None
    project_slug: str | None = None
    jira_issue_key: str | None = None
    repositories: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True


class CallIdentity(BaseModel):
    """Resolved identity for one MCP call (set by middleware, read by handlers)."""

    token_kind: TokenKind
    token_id: str
    agent_name: str
    acms_agent_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    # assignment-scoped extras (None for agent tokens)
    work_uid: str | None = None
    project_id: str | None = None
    project_slug: str | None = None
    jira_issue_key: str | None = None
    repositories: list[str] = Field(default_factory=list)

    @property
    def is_assignment_scoped(self) -> bool:
        return self.token_kind == TokenKind.ASSIGNMENT

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


class AuditEntry(BaseModel):
    """MCP activity log row (plan §24) — no secrets, arguments HASHED only."""

    ts: datetime = Field(default_factory=utcnow)
    agent_name: str
    agent_id: str | None = None
    token_id: str
    token_kind: str
    work_uid: str | None = None
    domain: str
    name: str  # resource/tool full name
    entry_kind: str  # resource | tool | capabilities | health
    risk_class: RiskClass = RiskClass.READ
    args_hash: str | None = None
    status: str  # ok | denied | error
    http_status: int | None = None
    duration_ms: int | None = None
    approval: str | None = None
    error_type: str | None = None
    detail: str | None = None
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


def hash_args(args: dict[str, Any] | None) -> str | None:
    """SHA-256 of canonical JSON args (plan §24: never store raw arguments)."""
    if not args:
        return None
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_raw_token(prefix: str) -> str:
    """Generate a raw bearer token. Raw shown ONCE to the operator; only the
    sha256 hash is stored (plan §12: never print tokens in logs/handoffs)."""
    return f"{prefix}_{uuid.uuid4().hex}{uuid.uuid4().hex[:12]}"
