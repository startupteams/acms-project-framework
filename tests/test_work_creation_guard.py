"""ADR-0011 (Accepted) Work Item creation authority tests."""

from __future__ import annotations

import pytest

from acms.work_creation_guard import evaluate_work_item_creation


@pytest.fixture(autouse=True)
def _executive(monkeypatch):
    monkeypatch.setenv("ACMS_EXECUTIVE_AGENT_IDS", "agent-exec-001, agent-exec-002")
    # Settings is lru_cached; clear so the fixture env is picked up.
    from acms.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_admin_may_create_without_claimed_agent():
    d = evaluate_work_item_creation(caller_is_admin=True, created_by="human:jordan")
    assert d.allowed and d.authority == "administrator"


def test_admin_claiming_worker_agent_identity_rejected():
    d = evaluate_work_item_creation(caller_is_admin=True, created_by="agent:worker-77")
    assert not d.allowed and d.authority == "worker"


def test_admin_claiming_executive_agent_identity_allowed():
    d = evaluate_work_item_creation(caller_is_admin=True, created_by="agent:agent-exec-001")
    assert d.allowed and d.authority == "administrator"


def test_worker_agent_cannot_create():
    d = evaluate_work_item_creation(caller_is_admin=False, created_by="agent:worker-77")
    assert not d.allowed and d.authority == "worker"


def test_executive_agent_may_create():
    d = evaluate_work_item_creation(caller_is_admin=False, created_by="agent:agent-exec-002")
    assert d.allowed and d.authority == "executive"


def test_unknown_provenance_rejected():
    d = evaluate_work_item_creation(caller_is_admin=False, created_by="mystery-source")
    assert not d.allowed and d.authority == "unknown"


def test_no_created_by_rejected_for_non_admin():
    d = evaluate_work_item_creation(caller_is_admin=False, created_by=None)
    assert not d.allowed


def test_empty_executive_config_blocks_all_agent_creation(monkeypatch):
    monkeypatch.setenv("ACMS_EXECUTIVE_AGENT_IDS", "")
    from acms.settings import get_settings

    get_settings.cache_clear()
    d = evaluate_work_item_creation(caller_is_admin=False, created_by="agent:agent-exec-001")
    assert not d.allowed
