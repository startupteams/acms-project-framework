"""W2 gateway tests: approvals, internal API, llm.*/runtime.* capabilities.

Covers plan §21 (llm/runtime surface), §13 (assignment auto-mint contract),
§16/§36 (approval workflow), §23/§24 (capability card + activity export).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mcp_gateway.approvals import APPROVED, DENIED, PENDING, ApprovalStore
from mcp_gateway.audit import AuditLog
from mcp_gateway.internal_api import InternalApi
from mcp_gateway.llm_adapter import ServerManagerClient
from mcp_gateway.models import utcnow
from mcp_gateway.tokens import TokenStore


# ---------------------------------------------------------------- approvals

def test_approval_create_and_list():
    with tempfile.TemporaryDirectory() as td:
        store = ApprovalStore(Path(td) / "a.sqlite3")
        out = store.create(agent_name="w-1", capability="runtime.restart_self",
                           reason="bridge wedged")
        assert out["status"] == PENDING
        rows = store.list(status=PENDING)
        assert len(rows) == 1 and rows[0]["capability"] == "runtime.restart_self"


def test_approval_grant_one_time_and_ttl():
    with tempfile.TemporaryDirectory() as td:
        store = ApprovalStore(Path(td) / "a.sqlite3")
        req = store.create(agent_name="w-1", capability="runtime.restart_self")
        # no grant yet
        assert store.check_and_consume_grant("w-1", "runtime.restart_self") is False
        store.decide(req["request_id"], decision=APPROVED, decided_by="stea-004")
        # first consume works
        assert store.check_and_consume_grant("w-1", "runtime.restart_self") is True
        # one-time: second consume fails
        assert store.check_and_consume_grant("w-1", "runtime.restart_self") is False
        row = store.get(req["request_id"])
        assert row["status"] == "USED"


def test_approval_grant_expiry():
    with tempfile.TemporaryDirectory() as td:
        store = ApprovalStore(Path(td) / "a.sqlite3")
        req = store.create(agent_name="w-2", capability="x.y")
        store.decide(req["request_id"], decision=APPROVED, decided_by="e", grant_ttl_minutes=0)
        # TTL 0 minutes: expires immediately-ish (within a second)
        import time as _t
        _t.sleep(1.1)
        assert store.check_and_consume_grant("w-2", "x.y") is False
        assert store.get(req["request_id"])["status"] == "EXPIRED"


def test_approval_grant_scoped_to_capability():
    with tempfile.TemporaryDirectory() as td:
        store = ApprovalStore(Path(td) / "a.sqlite3")
        req = store.create(agent_name="w-3", capability="a.b")
        store.decide(req["request_id"], decision=APPROVED, decided_by="e")
        # different capability or agent: NOT authorized
        assert store.check_and_consume_grant("w-3", "other.cap") is False
        assert store.check_and_consume_grant("w-99", "a.b") is False
        assert store.check_and_consume_grant("w-3", "a.b") is True


def test_approval_denial_never_grants():
    with tempfile.TemporaryDirectory() as td:
        store = ApprovalStore(Path(td) / "a.sqlite3")
        req = store.create(agent_name="w-4", capability="a.b")
        store.decide(req["request_id"], decision=DENIED, decided_by="e")
        assert store.check_and_consume_grant("w-4", "a.b") is False


def test_approval_decide_idempotent():
    with tempfile.TemporaryDirectory() as td:
        store = ApprovalStore(Path(td) / "a.sqlite3")
        req = store.create(agent_name="w-5", capability="a.b")
        first = store.decide(req["request_id"], decision=APPROVED, decided_by="e1")
        again = store.decide(req["request_id"], decision=DENIED, decided_by="e2")
        assert again["decided_by"] == "e1"  # first decision stands


# ---------------------------------------------------------------- internal API

class _FakeSM:
    """Fake ServerManagerClient for mint/capability-card flows."""

    def __init__(self):
        self.calls = []

    def request(self, method, path, query=None):
        self.calls.append((method, path, query))
        if path == "/api/v1/model-routes":
            return 200, {"routes": {"m-a": [{"engine": "vllm", "health": "healthy",
                                             "routable": True, "is_alias": False,
                                             "context_limit": 70000}]}}
        return 404, None


def _make_internal_api(tmpdir, *, agent_token: bool = True):
    tokens = TokenStore(Path(tmpdir) / "t.sqlite3")
    audit = AuditLog(Path(tmpdir) / "act.sqlite3")
    approvals = ApprovalStore(Path(tmpdir) / "a.sqlite3")
    api = InternalApi(tokens=tokens, audit=audit, approvals=approvals,
                      internal_token="secret-123")
    raw_agent = None
    if agent_token:
        _, raw_agent = tokens.mint_agent_token("acms-hermes-worker-uid-001",
                                               acms_agent_id="agent-uuid-1")
    return api, tokens, audit, approvals, raw_agent


async def test_internal_mint_requires_secret():
    with tempfile.TemporaryDirectory() as td:
        api, *_ = _make_internal_api(td, agent_token=False)
        sent = {}

        async def receive():
            return {"type": "http.request", "body": json.dumps({
                "agent_name": "acms-hermes-worker-uid-001",
                "work_uid": "ACMS-WORK-000001-20260101_000000"}).encode(), "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                sent["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                sent.setdefault("body", b"") + msg["body"]
                sent["body"] = sent.get("body", b"") + msg["body"]

        scope = {"type": "http", "path": "/internal/mint-assignment",
                 "method": "POST", "headers": [], "query_string": b""}
        await api(scope, receive, send)
        assert sent["status"] == 401


async def test_internal_mint_happy_path_and_unknown_agent():
    with tempfile.TemporaryDirectory() as td:
        api, tokens, audit, approvals, _raw = _make_internal_api(td)

        def _scope():
            return {"type": "http", "path": "/internal/mint-assignment", "method": "POST",
                    "headers": [(b"authorization", b"Bearer secret-123")],
                    "query_string": b""}

        body = {"agent_name": "acms-hermes-worker-uid-001",
                "work_uid": "ACMS-WORK-000001-20260101_000000", "ttl_hours": 8,
                "jira_issue_key": "STNA-89"}
        payload = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        sent = {}

        async def send(msg):
            if msg["type"] == "http.response.start":
                sent["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                sent["body"] = sent.get("body", b"") + msg["body"]

        await api(_scope(), receive, send)
        assert sent["status"] == 201
        out = json.loads(sent["body"])
        assert out["agent_name"] == "acms-hermes-worker-uid-001"
        assert out["work_uid"] == "ACMS-WORK-000001-20260101_000000"
        assert out["token"].startswith("mcpt_")
        # unknown agent: fail-closed 404
        sent.clear()
        payload2 = json.dumps({"agent_name": "ghost-agent",
                               "work_uid": "ACMS-WORK-000001-20260101_000000"}).encode()

        async def receive2():
            return {"type": "http.request", "body": payload2, "more_body": False}

        await api(_scope(), receive2, send)
        assert sent["status"] == 404


async def test_internal_capability_card_and_activity():
    with tempfile.TemporaryDirectory() as td:
        api, tokens, audit, approvals, _raw = _make_internal_api(td)
        scope = {"type": "http", "path": "/internal/capability-card/acms-hermes-worker-uid-001",
                 "method": "GET", "headers": [(b"authorization", b"Bearer secret-123")],
                 "query_string": b""}
        sent = {}

        async def send(msg):
            if msg["type"] == "http.response.start":
                sent["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                sent["body"] = sent.get("body", b"") + msg["body"]

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await api(scope, receive, send)
        assert sent["status"] == 200
        card = json.loads(sent["body"])
        assert card["agent_name"] == "acms-hermes-worker-uid-001"
        assert card["mcp_connected"] is True
        assert "runtime.restart_self" in card["tools_requiring_approval"]
        # no raw token ever in the card ("mcp_" prefix is the raw-token scheme)
        body_str = sent["body"].decode()
        assert "expires_at" in body_str  # metadata present
        import re as _re

        assert not _re.search(r'"token"\s*:\s*"mcp_[0-9a-f]', body_str)
        assert not _re.search(r'"token"\s*:\s*"mcpt_[0-9a-f]', body_str)


# ---------------------------------------------------------------- llm adapter

def test_sm_client_request_success_and_http_error():
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer good":
                self.send_response(401)
                self.end_headers()
                return
            if self.path.startswith("/api/v1/model-routes"):
                body = json.dumps({"routes": {"m": [{"engine": "vllm", "health": "healthy"}]}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = ServerManagerClient(f"http://127.0.0.1:{port}", "good")
        status, body = client.request("GET", "/api/v1/model-routes")
        assert status == 200 and "m" in body["routes"]
        # 404 → no exception, parsed body
        status, _ = client.request("GET", "/nope")
        assert status == 404
        # unreachable → DomainUnavailableError
        from mcp_gateway.errors import DomainUnavailableError
        bad = ServerManagerClient("http://127.0.0.1:59999", "good", timeout_seconds=1)
        with pytest.raises(DomainUnavailableError):
            bad.request("GET", "/api/v1/model-routes")
    finally:
        srv.shutdown()