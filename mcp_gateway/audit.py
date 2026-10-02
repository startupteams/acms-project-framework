"""Durable MCP activity log (plan §24).

SQLite table ``mcp_activity`` in the gateway store; JSONL mirror file
(``activity.jsonl``) for easy tail/grepping. Every call (ok/denied/error) is
recorded BEFORE any exception propagates. Secrets never enter this log:
arguments are hashed, results are not persisted at all.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .models import AuditEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_activity (
    ts TEXT NOT NULL,
    request_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    agent_id TEXT,
    token_id TEXT NOT NULL,
    token_kind TEXT NOT NULL,
    work_uid TEXT,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    args_hash TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    duration_ms INTEGER,
    approval TEXT,
    error_type TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_mcp_activity_ts ON mcp_activity(ts);
CREATE INDEX IF NOT EXISTS ix_mcp_activity_agent ON mcp_activity(agent_name);
CREATE INDEX IF NOT EXISTS ix_mcp_activity_work ON mcp_activity(work_uid);
CREATE INDEX IF NOT EXISTS ix_mcp_activity_name ON mcp_activity(name);
CREATE INDEX IF NOT EXISTS ix_mcp_activity_status ON mcp_activity(status);
"""


class AuditLog:
    def __init__(self, path: str | Path, *, mirror_jsonl: Path | None = None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._mirror = mirror_jsonl
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def record(self, entry: AuditEntry) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO mcp_activity (ts, request_id, agent_name, agent_id, "
                "token_id, token_kind, work_uid, domain, name, entry_kind, risk_class, "
                "args_hash, status, http_status, duration_ms, approval, error_type, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry.ts.isoformat(), entry.request_id, entry.agent_name, entry.agent_id,
                 entry.token_id, entry.token_kind, entry.work_uid, entry.domain, entry.name,
                 entry.entry_kind, entry.risk_class.value, entry.args_hash, entry.status,
                 entry.http_status, entry.duration_ms, entry.approval, entry.error_type,
                 (entry.detail or "")[:2000]),
            )
        if self._mirror is not None:
            self._mirror.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._mirror.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.model_dump(mode="json"), separators=(",", ":")) + "\n")

    def recent(self, limit: int = 100, *, agent_name: str | None = None,
               work_uid: str | None = None, status: str | None = None,
               denied_only: bool = False) -> list[dict]:
        q = "SELECT * FROM mcp_activity"
        clauses, params = [], []
        if agent_name:
            clauses.append("agent_name=?")
            params.append(agent_name)
        if work_uid:
            clauses.append("work_uid=?")
            params.append(work_uid)
        if status:
            clauses.append("status=?")
            params.append(status)
        if denied_only:
            clauses.append("status='denied'")
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM mcp_activity").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
