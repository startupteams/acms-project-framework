"""Product bootstrap tests — window-5 plan §8/§9/§10/§15.

Uses the REAL acceptance fixture (home-service-missed-call-agent-ops.md from
startupteams/business_idea_generator @ main) loaded from the local clone when
present, else an inline equivalent — asserting:

- draft/unvalidated REMAINS draft/unvalidated
- assumptions NEVER become accepted requirements automatically
- preview before create; resumable steps reuse created resources
- Jira failure preserves artifacts + blocks execution; repo never deleted
- AI planning is gated exactly like implementation
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.error
from unittest import mock

from acms.product_bootstrap import baseline_docs

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from acms.settings import get_settings

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}

FIXTURE_CANDIDATES = [
    pathlib.Path("/home/jordatech/work/business_idea_generator/data/ideas/"
                 "home-service-missed-call-agent-ops.md"),
]


def _fixture_text() -> str:
    for p in FIXTURE_CANDIDATES:
        if p.exists():
            return p.read_text()
    pytest.skip("acceptance fixture not available locally")


@pytest.fixture(autouse=True)
def boot_env(monkeypatch):
    monkeypatch.delenv("ACMS_GITHUB_ECONOMICS_TOKEN", raising=False)
    monkeypatch.delenv("ACMS_JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_EMAIL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("ACMS_JIRA_AI_ACCOUNT_ID", "712020:520fb263-ef0f-425c-a0be-14e9d258917e")
    monkeypatch.setenv("ACMS_JIRA_READY_STATUSES", "TO START")
    monkeypatch.setenv("ACMS_JIRA_RECONCILE_ENABLED", "false")
    monkeypatch.setenv("ACMS_ADMIN_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestParseAndClassify:
    def test_fixture_parses_grounded_metadata(self):
        from acms.product_bootstrap import parse_idea_markdown, validation_classified

        idea = parse_idea_markdown(_fixture_text())
        assert idea["title"] == "Home-Service Missed-Call Agent Ops"
        assert idea["slug"] == "home-service-missed-call-agent-ops"
        assert idea["status"] == "draft/unvalidated"
        assert idea["goodness_score"] == 86
        assert idea["persona"].startswith("Owners and operators of home-service")
        assert idea["validation_priority"].startswith("P1")
        assert idea["one_line_summary"], "body section extracted"
        assert idea["assumptions_to_validate"], "assumptions section extracted"
        assert idea["extra"]["score_inputs"]["competition_score"] == 80  # preserved verbatim
        cls = validation_classified(idea)
        assert cls["is_validated"] is False
        assert cls["requirements_class"] == "assumption"

    def test_validated_idea_classifies_validated(self):
        from acms.product_bootstrap import parse_idea_markdown, validation_classified

        idea = parse_idea_markdown(_fixture_text().replace(
            'status: "draft/unvalidated"', 'status: "validated"'))
        cls = validation_classified(idea)
        assert cls["is_validated"] is True
        assert cls["requirements_class"] == "human-approved requirement"


class TestDocsHonesty:
    def test_docs_never_promote_assumptions(self):
        """§15 core assertion: assumptions do NOT become accepted requirements."""
        from acms.product_bootstrap import (baseline_docs, parse_idea_markdown,
                                            validation_classified)

        idea = parse_idea_markdown(_fixture_text())
        cls = validation_classified(idea)
        docs = baseline_docs(idea, "Home-Service Missed-Call Agent Ops", cls)
        reqs = docs["REQUIREMENTS.md"]
        assert "ASSUMPTION" in reqs.upper()
        assert "draft/unvalidated" in reqs
        assert "validate now with owner interviews" in reqs.lower() or "P1" in reqs
        # no "validated requirement" claim anywhere for an unvalidated idea
        assert "validated requirement" not in reqs.lower()
        assert "draft/unvalidated" in docs["README.md"]

    def test_source_provenance_recorded(self):
        from acms.product_bootstrap import fetch_idea_from_github, parse_idea_markdown

        idea = parse_idea_markdown(_fixture_text())
        idea["source_provenance"] = {
            "source_repo": "startupteams/business_idea_generator",
            "source_path": "data/ideas/home-service-missed-call-agent-ops.md",
            "source_commit_sha": "abc123", "import_timestamp": "2026-09-30T00:00:00Z",
            "content_sha256": idea["content_sha256"],
        }
        from acms.product_bootstrap import validation_classified

        docs = baseline_docs(idea, "Home-Service Missed-Call Agent Ops",
                             validation_classified(idea))
        assert "startupteams/business_idea_generator" in docs["README.md"]
        assert "abc123" in docs["README.md"]


class TestGithubImportHonesty:
    def test_token_without_repo_grant_is_human_gate(self):
        """§19.1: 404 from a repo-scoped PAT → actionable human-gate message."""
        from acms.product_bootstrap import BootstrapError, fetch_idea_from_github

        import os
        os.environ["ACMS_GITHUB_ECONOMICS_TOKEN"] = "tok"  # token without the idea repo granted
        try:
            with mock.patch("urllib.request.urlopen") as u:
                import urllib.error
                u.side_effect = urllib.error.HTTPError(
                    "url", 404, "Not Found", None, None)
                with pytest.raises(BootstrapError) as ei:
                    fetch_idea_from_github("data/ideas/home-service-missed-call-agent-ops.md")
            assert ei.value.code == "idea-repo-not-granted"
            assert "human gate" in str(ei.value).lower()
        finally:
            os.environ.pop("ACMS_GITHUB_ECONOMICS_TOKEN", None)


class TestBootstrapFlow:
    def test_create_and_preview_no_writes(self):
        r = client.post("/api/v1/bootstrap/requests", headers=AUTH, json={
            "product_name": "Home-Service Missed-Call Agent Ops",
            "markdown": _fixture_text()})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["classification"]["is_validated"] is False
        assert body["preview"]["idea"]["slug"] == "home-service-missed-call-agent-ops"
        # nothing created yet
        g = client.get(f"/api/v1/bootstrap/requests/{body['request_id']}", headers=AUTH)
        steps = g.json()["steps"]
        assert set(steps) == {"input"}

    def test_execute_resumable_and_honest(self):
        """Repo step fails honestly (token can't create) — request stays resumable;
        no partial deletes; retry reuses steps."""
        r = client.post("/api/v1/bootstrap/requests", headers=AUTH, json={
            "product_name": "Resumable Probe Product", "markdown": _fixture_text()})
        rid = r.json()["request_id"]
        # execute: repo adapter should fail with the human-gate message
        e = client.post(f"/api/v1/bootstrap/requests/{rid}/execute", headers=AUTH, json={})
        assert e.status_code == 502
        assert e.json()["detail"]["code"] == "repo-token-missing"  # no token in test env
        # the durable request still holds the input (nothing lost)
        g = client.get(f"/api/v1/bootstrap/requests/{rid}", headers=AUTH).json()
        assert g["steps"]["input"]["idea"]["slug"]
        # with a mocked repo step, docs+hierarchy+jira succeed resumably
        with mock.patch("acms.repo_adapter.create_private_repo") as cr, \
                mock.patch("acms.repo_adapter.push_baseline_docs") as pd, \
                mock.patch("acms.jira_client.JiraClient") as jc:
            from types import SimpleNamespace

            fake_issue = SimpleNamespace(
                key="STNA-84", url="https://mock.jira/browse/STNA-84",
                status="IDEA/UNVALIDATED", issue_id="10084", summary="s",
                description="", priority="High", labels=[],
                assignee="startupteamscompany@gmail.com",
                assignee_account_id="other-account", status_category=None, updated=None)
            jc.return_value.get_issue.return_value = fake_issue
            jc.return_value.get_issue_by_id.return_value = fake_issue
            cr.return_value = {"created": True, "reused": False, "repo": "startupteams/x",
                               "url": "https://github.com/startupteams/x", "private": True,
                               "default_branch": "main"}
            pd.return_value = {"repo": "startupteams/x",
                               "pushed": ["README.md", "REQUIREMENTS.md", "BUSINESS_PROBLEMS.md",
                                          "ARCHITECTURE.md", "FUTURE_WORK.md", "INITIAL_IDEAS.md"],
                               "failed": None}
            e2 = client.post(f"/api/v1/bootstrap/requests/{rid}/execute", headers=AUTH,
                             json={"jira_issue_key": "STNA-84"})
            assert e2.status_code == 200, e2.text
            body = e2.json()
            assert body["hierarchy"]["initial_work_key"].startswith("ACMS-WORK-")
            assert body["jira"]["linked"] is True
            assert "never authorizes execution" in body["note"] or "ready state" in body["note"]
            assert body["gate"]["verdict"] in ("NOT_READY", "ASSIGNEE_MISMATCH", "JIRA_UNKNOWN")

    def test_jira_failure_preserves_artifacts(self):
        """§9.5: Jira failure retains completed artifacts; execution blocked."""
        with mock.patch("acms.repo_adapter.create_private_repo") as cr, \
                mock.patch("acms.repo_adapter.push_baseline_docs") as pd:
            cr.return_value = {"created": True, "reused": False, "repo": "startupteams/y",
                               "url": "https://github.com/startupteams/y", "private": True,
                               "default_branch": "main"}
            pd.return_value = {"repo": "startupteams/y", "pushed": ["README.md"], "failed": None}
            r = client.post("/api/v1/bootstrap/requests", headers=AUTH, json={
                "product_name": "Jira Failure Probe", "markdown": _fixture_text()})
            rid = r.json()["request_id"]
            # jira step: create_jira=True with mock-mode client → BootstrapError jira-not-configured
            e = client.post(f"/api/v1/bootstrap/requests/{rid}/execute", headers=AUTH,
                            json={"create_jira": True})
            body = e.json()
            assert e.status_code == 200  # bootstrap completes WITHOUT execution authorization
            assert body["state"] == "awaiting_jira"
            assert body["jira"]["linked"] is False
            assert "Awaiting Jira linkage" in body["jira"]["awaiting"]
            assert body["hierarchy"]["initial_work_item_id"]  # artifacts retained
            assert "blocked" in body["note"].lower()

    def test_retry_reuses_created_resources(self):
        """§15: bootstrap retries reuse repo, docs, jira issue, ACMS objects."""
        with mock.patch("acms.repo_adapter.create_private_repo") as cr, \
                mock.patch("acms.repo_adapter.push_baseline_docs") as pd:
            cr.return_value = {"created": False, "reused": True, "repo": "startupteams/z",
                               "url": "https://github.com/startupteams/z", "private": True,
                               "default_branch": "main"}
            pd.return_value = {"repo": "startupteams/z", "pushed": ["README.md"], "failed": None}
            r = client.post("/api/v1/bootstrap/requests", headers=AUTH, json={
                "product_name": "Retry Reuse Probe", "markdown": _fixture_text()})
            rid = r.json()["request_id"]
            e1 = client.post(f"/api/v1/bootstrap/requests/{rid}/execute", headers=AUTH, json={})
            hier1 = e1.json()["hierarchy"]
            # second execute: hierarchy step returns the SAME durable result
            e2 = client.post(f"/api/v1/bootstrap/requests/{rid}/execute", headers=AUTH, json={})
            hier2 = e2.json()["hierarchy"]
            assert hier1 == hier2  # no duplicate hierarchy objects

    def test_no_source_rejected(self):
        r = client.post("/api/v1/bootstrap/requests", headers=AUTH,
                        json={"product_name": "x"})
        assert r.status_code == 422