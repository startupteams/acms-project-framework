"""Durable gateway token store (plan §12/§13).

Design: the gateway keeps its OWN SQLite token store (``tokens.sqlite3``) so
that gateway identity is fully independent of ACMS schema/migrations (the
gateway stays additive; ACMS keeps zero MCP tables in W1). Store is
process-safe for the single gateway service process; writes are transactional.

Raw tokens are NEVER persisted — only sha256 hashes. ``mint_agent_token`` /
``mint_assignment_token`` return the raw token exactly once to the operator.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import (
    AgentTokenRecord,
    AssignmentTokenRecord,
    new_raw_token,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    agent_name TEXT NOT NULL,
    acms_agent_id TEXT,
    roles_json TEXT NOT NULL DEFAULT '[]',
    scopes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    revoked_reason TEXT,
    source_ip_allowlist_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS assignment_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    agent_name TEXT NOT NULL,
    work_uid TEXT NOT NULL,
    project_id TEXT,
    project_slug TEXT,
    jira_issue_key TEXT,
    repositories_json TEXT NOT NULL DEFAULT '[]',
    tools_json TEXT NOT NULL DEFAULT '[]',
    scopes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    revoked_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_tokens_hash ON agent_tokens(token_hash);
CREATE INDEX IF NOT EXISTS ix_assignment_tokens_hash ON assignment_tokens(token_hash);
CREATE INDEX IF NOT EXISTS ix_assignment_tokens_agent ON assignment_tokens(agent_name, work_uid);
"""


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return list(parsed) if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _row_to_agent(row: sqlite3.Row) -> AgentTokenRecord:
    return AgentTokenRecord(
        token_id=row["token_id"],
        token_hash=row["token_hash"],
        agent_name=row["agent_name"],
        acms_agent_id=row["acms_agent_id"],
        roles=_parse(row["roles_json"]),
        scopes=_parse(row["scopes_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
        revoked_reason=row["revoked_reason"],
        source_ip_allowlist=_parse(row["source_ip_allowlist_json"]),
    )


def _row_to_assignment(row: sqlite3.Row) -> AssignmentTokenRecord:
    return AssignmentTokenRecord(
        token_id=row["token_id"],
        token_hash=row["token_hash"],
        agent_name=row["agent_name"],
        work_uid=row["work_uid"],
        project_id=row["project_id"],
        project_slug=row["project_slug"],
        jira_issue_key=row["jira_issue_key"],
        repositories=_parse(row["repositories_json"]),
        tools=_parse(row["tools_json"]),
        scopes=_parse(row["scopes_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
        revoked_reason=row["revoked_reason"],
    )


class TokenStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            #defense: never inherit a non-database file
            probe = path.read_bytes()[:16] if path.stat().st_size >= 16 else b""
            if probe and not probe.startswith(b"SQLite format 3"):
                raise ValueError(f"token store path {path} exists and is not a SQLite database")
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------------- mint (raw returned ONCE) ----------------

    def mint_agent_token(
        self, agent_name: str, *, acms_agent_id: str | None = None,
        roles: list[str] | None = None, scopes: list[str] | None = None,
        ttl_days: int | None = 365, source_ip_allowlist: list[str] | None = None,
    ) -> tuple[AgentTokenRecord, str]:
        raw = new_raw_token("mcp")
        now = utcnow()
        rec = AgentTokenRecord(
            token_hash=_hash(raw),
            agent_name=agent_name,
            acms_agent_id=acms_agent_id,
            roles=roles or ["worker"],
            scopes=scopes or ["acms.read", "acms.write"],
            created_at=now,
            expires_at=now + timedelta(days=ttl_days) if ttl_days is not None else None,
            source_ip_allowlist=source_ip_allowlist or [],
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO agent_tokens (token_id, token_hash, agent_name, acms_agent_id, "
                "roles_json, scopes_json, created_at, expires_at, source_ip_allowlist_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (rec.token_id, rec.token_hash, rec.agent_name, rec.acms_agent_id,
                 json.dumps(rec.roles), json.dumps(rec.scopes), _iso(rec.created_at),
                 _iso(rec.expires_at), json.dumps(rec.source_ip_allowlist)),
            )
        return rec, raw

    def mint_assignment_token(
        self, *, agent_name: str, work_uid: str, agent_token_raw: str | None = None,
        agent_token_hash: str | None = None, ttl_hours: int = 24,
        project_id: str | None = None, project_slug: str | None = None,
        jira_issue_key: str | None = None, repositories: list[str] | None = None,
        tools: list[str] | None = None, scopes: list[str] | None = None,
    ) -> tuple[AssignmentTokenRecord, str]:
        """Mint an assignment token. Authenticated by EITHER the agent's raw token
        (verified here — never stored) or its hash (operator path)."""
        if agent_token_raw:
            agent_rec = self.resolve_agent_token(agent_token_raw)
            if agent_rec is None:
                raise PermissionError("agent token not recognized")
            if agent_rec.agent_name != agent_name:
                raise PermissionError("agent token does not belong to the named agent")
        elif agent_token_hash:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM agent_tokens WHERE token_hash=?", (agent_token_hash,)
                ).fetchone()
            if row is None:
                raise PermissionError("agent token not recognized")
            agent_rec = _row_to_agent(row)
            if agent_rec.agent_name != agent_name:
                raise PermissionError("agent token does not belong to the named agent")
        else:
            raise PermissionError("mint_assignment_token requires the agent's raw token or its hash")

        raw = new_raw_token("mcpt")
        now = utcnow()
        rec = AssignmentTokenRecord(
            token_hash=_hash(raw),
            agent_name=agent_name,
            work_uid=work_uid,
            project_id=project_id,
            project_slug=project_slug,
            jira_issue_key=jira_issue_key,
            repositories=repositories or [],
            tools=tools or [],
            scopes=scopes or ["acms.read", "acms.write"],
            created_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO assignment_tokens (token_id, token_hash, agent_name, work_uid, "
                "project_id, project_slug, jira_issue_key, repositories_json, tools_json, "
                "scopes_json, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.token_id, rec.token_hash, rec.agent_name, rec.work_uid, rec.project_id,
                 rec.project_slug, rec.jira_issue_key, json.dumps(rec.repositories),
                 json.dumps(rec.tools), json.dumps(rec.scopes), _iso(rec.created_at),
                 _iso(rec.expires_at)),
            )
        return rec, raw

    # ---------------- resolution / lifecycle ----------------

    def resolve_agent_token(self, raw: str) -> AgentTokenRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_tokens WHERE token_hash=?", (_hash(raw),)
            ).fetchone()
        return _row_to_agent(row) if row else None

    def resolve_assignment_token(self, raw: str) -> AssignmentTokenRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assignment_tokens WHERE token_hash=?", (_hash(raw),)
            ).fetchone()
        return _row_to_assignment(row) if row else None

    def list_agent_tokens(self, *, include_revoked: bool = True) -> list[AgentTokenRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_tokens ORDER BY created_at DESC"
            ).fetchall()
        out = [_row_to_agent(r) for r in rows]
        if not include_revoked:
            out = [t for t in out if t.is_active()]
        return out

    def list_assignment_tokens(self, *, agent_name: str | None = None,
                               work_uid: str | None = None,
                               include_revoked: bool = True) -> list[AssignmentTokenRecord]:
        q = "SELECT * FROM assignment_tokens"
        clauses, params = [], []
        if agent_name:
            clauses.append("agent_name=?")
            params.append(agent_name)
        if work_uid:
            clauses.append("work_uid=?")
            params.append(work_uid)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = [_row_to_assignment(r) for r in rows]
        if not include_revoked:
            out = [t for t in out if t.is_active()]
        return out

    def revoke_agent_token(self, token_id: str, reason: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE agent_tokens SET revoked_at=?, revoked_reason=? "
                "WHERE token_id=? AND revoked_at IS NULL",
                (_iso(utcnow()), reason, token_id),
            )
        return cur.rowcount > 0

    def revoke_assignment_token(self, token_id: str, reason: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE assignment_tokens SET revoked_at=?, revoked_reason=? "
                "WHERE token_id=? AND revoked_at IS NULL",
                (_iso(utcnow()), reason, token_id),
            )
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
