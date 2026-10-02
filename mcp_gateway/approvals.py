"""Durable SENSITIVE_WRITE approval requests (plan §16/§36; ADR-0021 W2).

When a worker calls a SENSITIVE_WRITE capability (e.g. runtime.restart_self is
worker-denied but a worker MAY legitimately need an elevated action later), the
policy engine records a durable APPROVAL REQUEST instead of a bare deny. The
request lands in the gateway-local SQLite store, is surfaced through:

- the operator CLI (``approval list`` / ``approval decide``), and
- the ACMS inbox (the caller's own ``acms.inbox.raise`` path OR the gateway
  internal API consumers), so humans see it where they already work.

SECURITY MODEL (ADR-0021, W2 semantics — unchanged hard rules):
- SENSITIVE_WRITE + executive/infrastructure_admin => allowed immediately
  (executives ARE the approval authority, plan §16).
- SENSITIVE_WRITE + worker => ApprovalRequiredError + durable request. The
  request NEVER auto-executes on approval: approval only flips the request
  status; an approved request grants a ONE-TIME, TTL-bounded approval grant
  that the SAME capability consults on the caller's NEXT call. Scope is exactly
  (agent_name, capability_name) — never a blanket elevation.
- DESTRUCTIVE => denied forever at the gateway (plan §43; not approval-able).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    capability TEXT NOT NULL,
    reason TEXT,
    args_hash TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    grant_expires_at TEXT,
    grant_used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_approval_agent ON approval_requests(agent_name, status);
CREATE INDEX IF NOT EXISTS ix_approval_status ON approval_requests(status);
"""

PENDING = "PENDING"
APPROVED = "APPROVED"
DENIED = "DENIED"
USED = "USED"
EXPIRED = "EXPIRED"


class ApprovalStore:
    """Gateway-local approval request store (single service process)."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # ---------------- create / list / decide ----------------

    def create(self, *, agent_name: str, capability: str, reason: str = "",
               args_hash: str | None = None,
               grant_ttl_minutes: int = 15) -> dict[str, Any]:
        req_id = f"apr-{uuid.uuid4().hex[:20]}"
        now = utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approval_requests (request_id, agent_name, capability, reason, "
                "args_hash, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (req_id, agent_name, capability, reason[:500], args_hash, PENDING,
                 now.isoformat()),
            )
        return {"request_id": req_id, "status": PENDING,
                "grant_ttl_minutes": grant_ttl_minutes,
                "created_at": now.isoformat()}

    def list(self, *, status: str | None = None,
             agent_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM approval_requests"
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if agent_name:
            clauses.append("agent_name=?")
            params.append(agent_name)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def decide(self, request_id: str, *, decision: str, decided_by: str,
               note: str = "", grant_ttl_minutes: int = 15) -> dict[str, Any] | None:
        """Record a human/executive decision. APPROVED arms a one-time grant
        bounded by grant_ttl_minutes (checked against grant_expires_at on use)."""
        if decision not in (APPROVED, DENIED):
            raise ValueError("decision must be APPROVED or DENIED")
        now = utcnow()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != PENDING:
                return dict(row)  # idempotent: already decided
            grant_exp = (
                (now + timedelta(minutes=grant_ttl_minutes)).isoformat()
                if decision == APPROVED else None
            )
            self._conn.execute(
                "UPDATE approval_requests SET status=?, decided_at=?, decided_by=?, "
                "decision_note=?, grant_expires_at=? WHERE request_id=?",
                (decision, now.isoformat(), decided_by, note[:500], grant_exp, request_id),
            )
        out = self.get(request_id)
        assert out is not None
        return out

    # ---------------- grant consumption (one-time, TTL-bounded) ----------------

    def check_and_consume_grant(self, agent_name: str, capability: str) -> bool:
        """True iff an APPROVED, unexpired, unused grant exists for exactly
        (agent_name, capability). Consumes it atomically when true."""
        now = utcnow().isoformat()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM approval_requests WHERE agent_name=? AND capability=? "
                "AND status=? AND grant_expires_at IS NOT NULL AND grant_used_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (agent_name, capability, APPROVED),
            ).fetchone()
            if row is None:
                return False
            if row["grant_expires_at"] <= now:
                self._conn.execute(
                    "UPDATE approval_requests SET status=? WHERE request_id=?",
                    (EXPIRED, row["request_id"]),
                )
                return False
            self._conn.execute(
                "UPDATE approval_requests SET grant_used_at=?, status=? WHERE request_id=?",
                (now, USED, row["request_id"]),
            )
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
