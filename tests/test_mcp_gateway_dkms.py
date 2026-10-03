"""P2 gateway tests: dkms.* capabilities (plan P2).

Covers: DKMS resources read, ingest_plan with HUMAN_APPROVED authority,
ingest_handoff creates TWO records + summarizes link, record_lesson works with
NO active assignment (STEA direct write path), secret-like content → QUARANTINED
(no retry), mark_training_eligibility denied for worker role (ROLE_REQUIRED)
but allowed for executive, unknown args denial, domain absent when env unconfigured.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.dkms_adapter import DkmsClient
from mcp_gateway.errors import QuarantinedContentError, RoleRequiredError
from mcp_gateway.models import CallIdentity, TokenKind
from mcp_gateway.server import GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore

from tests.test_mcp_gateway_w4 import _asyncio_run, FakeAcms, make_identity


class FakeDkmsClient(DkmsClient):
    """Canned DKMS surfaces for testing. No network."""

    def __init__(self, *, raise_quarantined: bool = False):
        super().__init__(base_url="http://127.0.0.1:30800", token="fake-dkms")
        self._raise_quarantined = raise_quarantined
        self._created_records: list[dict] = []
        self._created_links: list[dict] = []
        self._created_failures: list[dict] = []
        self._recovery_attempts: list[tuple[str, dict]] = []
        self._training_updates: list[tuple[str, dict]] = []

    def health(self):
        return {"status": "ok"}

    def get_record(self, uid: str):
        return {"knowledge_uid": uid, "record_type": "plan", "title": "Test Plan",
                "body_markdown": "# Test", "authority_class": "HUMAN_APPROVED"}

    def list_records(self, filters: dict):
        limit = int(filters.get("limit", 20))
        return [{"knowledge_uid": f"rec-{i}", "record_type": "plan",
                 "title": f"Record {i}"} for i in range(min(limit, 5))]

    def search(self, q: str, limit: int = 20):
        return [{"knowledge_uid": "search-1", "title": f"Search result for {q}"}]

    def related(self, uid: str):
        return [{"knowledge_uid": f"rel-{uid}", "link_type": "derived_from"}]

    def create_record(self, payload: dict):
        if self._raise_quarantined:
            raise QuarantinedContentError("content quarantined (secret-like material detected)")
        uid = f"rec-{len(self._created_records) + 1}"
        self._created_records.append(payload)
        return {"knowledge_uid": uid, **payload}

    def create_link(self, payload: dict):
        self._created_links.append(payload)
        return {"link_uid": f"link-{len(self._created_links)}", **payload}

    def create_failure(self, payload: dict):
        self._created_failures.append(payload)
        return {"failure_uid": f"fail-{len(self._created_failures)}", **payload}

    def create_recovery_attempt(self, failure_uid: str, payload: dict):
        self._recovery_attempts.append((failure_uid, payload))
        return {"attempt_uid": f"attempt-{len(self._recovery_attempts)}", **payload}

    def set_training_eligibility(self, uid: str, payload: dict):
        self._training_updates.append((uid, payload))
        return {"uid": uid, **payload}

    def export_markdown(self, query: dict):
        return "# Exported Markdown\n\nContent here."

    def audit(self):
        return [{"event_id": "evt-1", "action": "create_record"}]


def build_server_at(tmp_path: Path, fake_dkms: FakeDkmsClient | None = None) -> GatewayServer:
    cfg = GatewayConfig(
        acms_base_url="http://127.0.0.1:9999", acms_token="fake",
        dkms_base_url="http://127.0.0.1:30800", dkms_token="fake-dkms",
        approvals_path=str(tmp_path / "approvals.sqlite3"),
    )
    tokens = TokenStore(str(tmp_path / "tokens.sqlite3"))
    audit = AuditLog(str(tmp_path / "activity.sqlite3"))
    return GatewayServer(cfg, tokens=tokens, audit=audit,
                         acms_client=FakeAcms(), llm_client=None,
                         dkms_client=fake_dkms or FakeDkmsClient())


def _read_resource(server, resource_name, identity):
    cap = server.registry._capabilities[resource_name]
    return _asyncio_run(cap.handler(identity, {}))


def _read_resource_with_params(server, resource_name, identity, params):
    cap = server.registry._capabilities[resource_name]
    return _asyncio_run(cap.handler(identity, params))


def _call_tool(server, tool_name, identity, **kwargs):
    from mcp_gateway.server import CURRENT_IDENTITY
    storage_key = tool_name.replace(".", "_")
    gt = server.mcp._tool_manager._tools[storage_key]

    async def _go():
        token = CURRENT_IDENTITY.set(identity)
        try:
            return await gt.run(dict(kwargs), context=None)
        finally:
            CURRENT_IDENTITY.reset(token)
    return _asyncio_run(_go())


def _exec_identity():
    return CallIdentity(token_kind=TokenKind.AGENT, token_id="t-exec",
                        agent_name="stea-004", acms_agent_id="a-exec",
                        roles=["executive"], scopes=["dkms.read", "dkms.write"])


def _infra_admin_identity():
    return CallIdentity(token_kind=TokenKind.AGENT, token_id="t-infra",
                        agent_name="infra-admin", acms_agent_id="a-infra",
                        roles=["infrastructure_admin"], scopes=["dkms.read", "dkms.write"])


def test_dkms_domain_absent_when_env_unconfigured(tmp_path):
    """DKMS domain absent when DKMS_BASE_URL/TOKEN missing → health omits dkms."""
    cfg = GatewayConfig(acms_base_url="http://127.0.0.1:9999", acms_token="fake",
                        approvals_path=str(tmp_path / "appr.sqlite3"))
    tokens = TokenStore(str(tmp_path / "t.sqlite3"))
    audit = AuditLog(str(tmp_path / "a.sqlite3"))
    server = GatewayServer(cfg, tokens=tokens, audit=audit,
                           acms_client=FakeAcms(), llm_client=None)
    names = {c.name for c in server.registry._capabilities.values()}
    assert not any(n.startswith("dkms.") for n in names)


def test_dkms_resources_read(tmp_path):
    """DKMS read resources: record.get, list.recent, search."""
    server = build_server_at(tmp_path, FakeDkmsClient())
    ident = make_identity()
    rec = _read_resource_with_params(server, "dkms.record.get", ident, {"uid": "rec-123"})
    assert rec["record"]["knowledge_uid"] == "rec-123"
    lst = _read_resource_with_params(server, "dkms.list.recent", ident, {"limit": "10"})
    assert len(lst["records"]) == 5
    search = _read_resource_with_params(server, "dkms.search", ident, {"query": "test"})
    assert len(search["results"]) == 1


def test_dkms_record_related(tmp_path):
    """dkms.record.related resource works."""
    server = build_server_at(tmp_path, FakeDkmsClient())
    ident = make_identity()
    rel = _read_resource_with_params(server, "dkms.record.related", ident, {"uid": "rec-xyz"})
    assert len(rel["related"]) == 1
    assert rel["related"][0]["link_type"] == "derived_from"


def test_ingest_plan_creates_with_human_approved_authority(tmp_path):
    """dkms.ingest_plan creates plan with HUMAN_APPROVED authority."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    out = _call_tool(server, "dkms_ingest_plan", ident,
                     title="Test Plan", body_markdown="# Plan body",
                     source_uri="file:///plan.md", source_uid="src-001")
    assert out["record_type"] == "plan"
    assert out["authority_class"] == "HUMAN_APPROVED"
    assert len(fake_dkms._created_records) == 1
    rec = fake_dkms._created_records[0]
    assert rec["record_type"] == "plan"
    assert rec["authority_class"] == "HUMAN_APPROVED"
    assert rec["retention_class"] == "standard-90d"


def test_ingest_handoff_creates_two_records_plus_link(tmp_path):
    """dkms.ingest_handoff creates TWO records (source + summary) + summarizes link."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    out = _call_tool(server, "dkms_ingest_handoff", ident,
                     title="Handoff", body_markdown="# Handoff content",
                     source_uri="file:///handoff.md", source_uid="src-002")
    assert "source_uid" in out and "summary_uid" in out
    assert out["link_type"] == "summarizes"
    assert len(fake_dkms._created_records) == 2
    assert len(fake_dkms._created_links) == 1
    link = fake_dkms._created_links[0]
    assert link["link_type"] == "summarizes"


def test_record_lesson_no_own_work_requirement(tmp_path):
    """dkms.record_lesson works with NO active assignment (STEA direct write path)
    and stamps agent_uid from identity."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    # identity with acms_agent_id but NO work_uid, no assignment
    ident = CallIdentity(token_kind=TokenKind.AGENT, token_id="t-plain",
                         agent_name="w-standalone", acms_agent_id="agent-standalone",
                         roles=["worker"], scopes=["dkms.read", "dkms.write"])
    out = _call_tool(server, "dkms_record_lesson", ident,
                     title="Lesson Learned", body_markdown="# Lesson",
                     source_uri="file:///lesson.md", source_uid="src-003")
    assert out["record_type"] == "lesson_learned"
    assert out["agent_uid"] == "agent-standalone"
    rec = fake_dkms._created_records[0]
    assert rec["agent_uid"] == "agent-standalone"
    assert rec["authority_class"] == "AGENT_INFERRED"
    assert rec["retention_class"] == "indefinite"


def test_quarantined_content_raises_no_retry(tmp_path):
    """Secret-like content → QUARANTINED error, NO retry or sanitization."""
    fake_dkms = FakeDkmsClient(raise_quarantined=True)
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    with pytest.raises(QuarantinedContentError) as exc_info:
        _call_tool(server, "dkms_ingest_plan", ident,
                   title="Secret Plan", body_markdown="AWS_SECRET_KEY=xxx",
                   source_uri="file:///secret.md", source_uid="src-secret")
    assert "quarantined" in str(exc_info.value).lower()


def test_mark_training_eligibility_denied_for_worker(tmp_path):
    """dkms.mark_training_eligibility denied for worker role (ROLE_REQUIRED)."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()  # worker role
    with pytest.raises(RoleRequiredError):
        _call_tool(server, "dkms_mark_training_eligibility", ident,
                   uid="rec-123", training_eligibility="approved",
                   training_approval_mode="human", reason="test")


def test_mark_training_eligibility_allowed_for_executive(tmp_path):
    """dkms.mark_training_eligibility allowed for executive."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = _exec_identity()
    out = _call_tool(server, "dkms_mark_training_eligibility", ident,
                     uid="rec-123", training_eligibility="approved",
                     training_approval_mode="human", reason="executive approval")
    assert out["updated"] is True
    assert len(fake_dkms._training_updates) == 1


def test_mark_training_eligibility_allowed_for_infra_admin(tmp_path):
    """dkms.mark_training_eligibility allowed for infrastructure_admin."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = _infra_admin_identity()
    out = _call_tool(server, "dkms_mark_training_eligibility", ident,
                     uid="rec-456", training_eligibility="denied",
                     training_approval_mode="auto_policy", reason="infra admin denial")
    assert out["updated"] is True


def test_unknown_args_denied_schema_validation(tmp_path):
    """Unknown args rejected by schema validation."""
    from mcp_gateway.errors import ValidationError_
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    with pytest.raises(ValidationError_) as exc_info:
        _call_tool(server, "dkms_ingest_plan", ident,
                   title="Test", body_markdown="# body",
                   source_uri="file:///x.md", source_uid="src",
                   unknown_field="should fail")
    assert "unknown" in str(exc_info.value).lower()


def test_record_failure_creates_failure(tmp_path):
    """dkms.record_failure creates a failure record."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    out = _call_tool(server, "dkms_record_failure", ident,
                     title="Build Failure",
                     failure_signature="npm ERR! code ERESOLVE",
                     failure_class="build_failure",
                     system="npm/node",
                     symptoms="npm install fails with ERESOLVE",
                     evidence="error log attached")
    assert out["recorded"] is True
    assert "failure_uid" in out
    assert len(fake_dkms._created_failures) == 1


def test_record_recovery_attempt_attaches_to_failure(tmp_path):
    """dkms.record_recovery_attempt attaches to existing failure."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    out = _call_tool(server, "dkms_record_recovery_attempt", ident,
                     failure_uid="fail-123",
                     method="npm cache clean --force && npm install",
                     result="success",
                     attempt_number=1)
    assert out["recorded"] is True
    assert len(fake_dkms._recovery_attempts) == 1
    assert fake_dkms._recovery_attempts[0][0] == "fail-123"


def test_dkms_export_markdown(tmp_path):
    """dkms.export.markdown resource works."""
    server = build_server_at(tmp_path, FakeDkmsClient())
    ident = make_identity()
    out = _read_resource_with_params(server, "dkms.export.markdown", ident, {"work_uid": "w-123"})
    assert "markdown" in out
    assert "Exported Markdown" in out["markdown"]


def test_dkms_audit_recent(tmp_path):
    """dkms.audit.recent resource works with bounded limit."""
    server = build_server_at(tmp_path, FakeDkmsClient())
    ident = make_identity()
    out = _read_resource(server, "dkms.audit.recent", ident)
    assert "events" in out
    assert len(out["events"]) <= 50


def test_ingest_plan_optional_fields(tmp_path):
    """dkms.ingest_plan passes optional fields correctly."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    _call_tool(server, "dkms_ingest_plan", ident,
               title="Plan with all fields", body_markdown="# full",
               source_uri="file:///plan.md", source_uid="src-full",
               work_uid="w-123", jira_key="STNA-99", project_uid="p-1",
               repository="org/repo", commit_sha="abc123")
    rec = fake_dkms._created_records[0]
    assert rec["work_uid"] == "w-123"
    assert rec["jira_key"] == "STNA-99"
    assert rec["repository"] == "org/repo"
    assert rec["commit_sha"] == "abc123"


def test_record_decision(tmp_path):
    """dkms.record_decision creates decision record."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    out = _call_tool(server, "dkms_record_decision", ident,
                     title="Architecture Decision",
                     body_markdown="# Use Postgres over MySQL",
                     source_uri="file:///adr.md", source_uid="adr-001")
    assert out["record_type"] == "decision"
    rec = fake_dkms._created_records[0]
    assert rec["record_type"] == "decision"
    assert rec["authority_class"] == "AGENT_INFERRED"


def test_record_timeline_entry(tmp_path):
    """dkms.record_timeline_entry creates implementation record."""
    fake_dkms = FakeDkmsClient()
    server = build_server_at(tmp_path, fake_dkms)
    ident = make_identity()
    out = _call_tool(server, "dkms_record_timeline_entry", ident,
                     title="Implemented auth middleware",
                     body_markdown="# Auth middleware added",
                     source_uid="timeline-001", work_uid="w-123")
    assert out["record_type"] == "implementation"
    rec = fake_dkms._created_records[0]
    assert rec["authority_class"] == "SYSTEM_OBSERVED"
    assert rec["retention_class"] == "indefinite"


def test_template_matching_handles_query_strings():
    """SDK ResourceTemplate.matches() breaks on '?key={var}' templates (bare ? becomes
    a regex quantifier). GatewayServer._patch_template_matching() must fix it globally
    (plan P2 live-found via real MCP resources/read)."""
    from mcp.server.fastmcp.resources import templates as tpl_mod

    class _Fake:
        uri_template = "dkms://search?q={query}"

    # apply the gateway patch (idempotent)
    GatewayServer._patch_template_matching()
    params = tpl_mod.ResourceTemplate.matches(_Fake(), "dkms://search?q=qga")
    assert params == {"query": "qga"}

    class _Fake2:
        uri_template = "dkms://records/recent?limit={limit}"

    params2 = tpl_mod.ResourceTemplate.matches(_Fake2(), "dkms://records/recent?limit=5")
    assert params2 == {"limit": "5"}

    # unchanged path-style templates still work
    class _Fake3:
        uri_template = "dkms://record/{uid}"

    params3 = tpl_mod.ResourceTemplate.matches(
        _Fake3(), "dkms://record/8c1a74405bcc4ed295b661dd2d0e4dce")
    assert params3 == {"uid": "8c1a74405bcc4ed295b661dd2d0e4dce"}
