"""Jira v1 integration contract tests (mock mode — no credentials, no network)."""

from __future__ import annotations

import pytest

from acms.jira_client import JiraClient, JiraError
from acms.jira_sync import JiraSyncService, correlation_tag, extract_jira_key


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ACMS_JIRA_BASE_URL", "")
    monkeypatch.setenv("ACMS_JIRA_EMAIL", "")
    monkeypatch.setenv("ACMS_JIRA_API_TOKEN", "")
    from acms.settings import get_settings

    get_settings.cache_clear()
    c = JiraClient()  # mock mode: no config present
    c.add_mock_issue("STL-42", "Ship P0 demo", "High Point demo adaptation", status="In Progress",
                     priority="High", labels=["speed-to-lead"])
    yield c
    get_settings.cache_clear()


def test_mock_mode_without_credentials(client):
    assert client.is_mock


def test_get_issue_mock(client):
    issue = client.get_issue("STL-42")
    assert issue.key == "STL-42"
    assert issue.summary == "Ship P0 demo"
    assert issue.status == "In Progress"


def test_get_issue_unknown_key(client):
    with pytest.raises(JiraError):
        client.get_issue("NOPE-1")


def test_project_allowlist_blocks(monkeypatch, client):
    monkeypatch.setattr(client, "allowed_projects", {"OTHER"})
    with pytest.raises(JiraError):
        client.get_issue("STL-42")


def test_add_comment_records_mock(client):
    cid = client.add_comment("STL-42", "[ACMS Executive] COMPLETED: P0 demo done")
    assert cid.startswith("mock-comment")
    assert any(call_["op"] == "add_comment" for call_ in client.mock_calls)


def test_status_mutation_disabled_by_default(client):
    with pytest.raises(JiraError, match="disabled"):
        client.transition_status("STL-42", "Done")


def test_status_mutation_enabled(monkeypatch, client):
    monkeypatch.setattr(client, "status_mutation_enabled", True)
    assert client.transition_status("STL-42", "Done") is True



async def test_inbound_sync_records_events(client):
    from acms.db import get_session
    from acms.jira_sync import JiraSyncService

    svc = JiraSyncService(client)
    async for db in get_session():
        issues = await svc.inbound_assigned_issues(db)
        assert len(issues) == 1
        assert issues[0]["correlation_tag"] == "jira:STL-42"
        break


def test_correlation_roundtrip():
    tag = correlation_tag("STL-42")
    assert extract_jira_key("Work for jira:STL-42 demo") == "STL-42"
    assert extract_jira_key(tag) == "STL-42"
    assert extract_jira_key("no correlation here") is None


def test_no_issue_creation_capability(client):
    """Invariant (plan D7): the client must have NO create-issue method."""
    assert not hasattr(client, "create_issue")
    assert not hasattr(client, "create")
