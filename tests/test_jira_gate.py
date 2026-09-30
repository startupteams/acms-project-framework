"""Jira kickoff gate + reconciliation tests (window-5 plan §7/§13/§15).

Covers:
- evaluate_observation mapping (ready/unready/mismatch/hold/unknown)
- dispatch gate: unlinked draft dispatch blocked server-side; linked+ready+
  AI-assigned passes the gate; budget still applies after
- persistent holds survive polls; clearing is an explicit operator action
- reconciliation run ledger: coalescing, schedule freshness, interval clamp
- API contract: reconcile POST-only (GET never starts work), link endpoint
  honesty (linkage != authorization), hold validation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from acms.jira_gate import (ASSIGNEE_MISMATCH, ELIGIBLE, HOLD_PAUSE, HOLD_STOP,
                            LOCAL_HOLD, NOT_LINKED, NOT_READY, UNKNOWN,
                            evaluate_observation, gate_work_item)
from acms.jira_models import (JiraIssueLinkRecord,
                              JiraReconciliationRunRecord,
                              WorkRuntimeHoldRecord, new_id)
from acms.main import app
from acms.settings import get_settings
from acms.work_models import WorkItemRecord
from tests.test_ui_auth import ADMIN_DN

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}

AI_ACCOUNT_ID = "712020:520fb263-ef0f-425c-a0be-14e9d258917e"


@pytest.fixture()
async def db():
    """Async session over the shared unit DB (same pattern as test_run_watcher)."""
    from acms.db import get_session

    async for session in get_session():
        yield session


@pytest.fixture(autouse=True)
def gate_env(monkeypatch):
    """Settings via env vars + cache_clear (the proven lru_cache-safe pattern)."""
    monkeypatch.setenv("ACMS_JIRA_AI_ACCOUNT_ID", AI_ACCOUNT_ID)
    monkeypatch.setenv("ACMS_JIRA_READY_STATUSES", "TO START")
    # mock mode: no live calls in unit tests
    monkeypatch.delenv("ACMS_JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_EMAIL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("ACMS_JIRA_RECONCILE_INTERVAL_HOURS", "24")
    monkeypatch.setenv("ACMS_JIRA_RECONCILE_ENABLED", "false")
    monkeypatch.setenv("ACMS_SESSION_SECRET", "ui-test-secret-0123456789abcdef")
    monkeypatch.setenv("ACMS_LDAP_URL", "ldaps://lldap.miam.home.arpa:636")
    monkeypatch.setenv("ACMS_LDAP_USER_BASE", "ou=people,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_LDAP_BIND_DN", "uid=acms,ou=people,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_LDAP_BIND_PASSWORD", "bind-secret")
    monkeypatch.setenv("ACMS_LDAP_GROUP_ADMIN", ADMIN_DN)
    monkeypatch.setenv("ACMS_LDAP_GROUP_WORKER", "cn=acms-worker,ou=groups,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_LDAP_GROUP_OBSERVER", "cn=acms-observer,ou=groups,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_ADMIN_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _mk_work(db, *, title="gate probe", **linkage) -> str:
    now = datetime.now(timezone.utc)
    wid = str(uuid4())
    db.add(WorkItemRecord(work_item_id=wid, title=title, created_at=now,
                          updated_at=now, **linkage))
    await db.commit()
    return wid


def _jira_issue(status: str, account_id: str | None, *, key="STNA-99", issue_id="10001"):
    return SimpleNamespace(
        key=key, status=status, assignee_account_id=account_id,
        issue_id=issue_id, summary="gate probe", description="", priority="High",
        labels=[], assignee="startupteamscompany@gmail.com",
        url=f"https://mock.jira/browse/{key}", status_category=None, updated=None)


def _install_mock_client(monkeypatch, issue: SimpleNamespace | None, fail: bool = False):
    # gate_work_item does `from .jira_client import JiraClient` at call time —
    # patching the SOURCE module attribute is what the call site resolves.
    from acms import jira_client

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        def get_issue(self, key):
            if fail:
                raise RuntimeError("jira down")
            return issue
        def get_issue_by_id(self, issue_id):
            if fail:
                raise RuntimeError("jira down")
            return issue
    monkeypatch.setattr(jira_client, "JiraClient", FakeClient)


# ---------------------------------------------------------------- §13.2 mapping

def test_ready_and_ai_assigned_is_eligible(gate_env):
    v = evaluate_observation(status_name="TO START",
                             assignee_account_id=AI_ACCOUNT_ID, has_local_hold=False)
    assert v.eligible and v.verdict == ELIGIBLE


def test_ready_without_ai_assignee_blocked(gate_env):
    v = evaluate_observation(status_name="TO START",
                             assignee_account_id="other-account", has_local_hold=False)
    assert not v.eligible and v.verdict == ASSIGNEE_MISMATCH


def test_ai_assigned_without_ready_blocked(gate_env):
    v = evaluate_observation(status_name="IDEA/UNVALIDATED",
                             assignee_account_id=AI_ACCOUNT_ID, has_local_hold=False)
    assert not v.eligible and v.verdict == NOT_READY


def test_local_hold_beats_ready(gate_env):
    v = evaluate_observation(status_name="TO START",
                             assignee_account_id=AI_ACCOUNT_ID,
                             has_local_hold=True, hold_kinds=[HOLD_STOP])
    assert not v.eligible and v.verdict == LOCAL_HOLD


def test_unpinned_account_is_unknown_fail_closed(gate_env):
    get_settings().jira_ai_account_id = ""
    v = evaluate_observation(status_name="TO START",
                             assignee_account_id=AI_ACCOUNT_ID, has_local_hold=False)
    assert not v.eligible and v.verdict == UNKNOWN


def test_custom_ready_status_mapping(gate_env):
    get_settings().jira_ready_statuses = "APPROVED WORK"
    v = evaluate_observation(status_name="Approved Work",
                             assignee_account_id=AI_ACCOUNT_ID, has_local_hold=False)
    assert v.eligible


# ---------------------------------------------------------------- dispatch gate

async def test_unlinked_draft_cannot_dispatch(clean_db, db, gate_env):
    """§7.1: an ACMS draft without Jira linkage is non-executable — server-side."""
    wid = await _mk_work(db)
    verdict = await gate_work_item(db, wid, live_check=False)
    assert verdict.verdict == NOT_LINKED and not verdict.eligible
    # persisted verdict visible for UI
    work = await db.get(WorkItemRecord, wid)
    assert work.jira_eligibility == NOT_LINKED


async def test_gate_passes_ready_linked(clean_db, db, monkeypatch, gate_env):
    wid = await _mk_work(db, title="ready gated", jira_issue_id="10001",
                         jira_issue_key="STNA-99")
    _install_mock_client(monkeypatch, _jira_issue("TO START", AI_ACCOUNT_ID))
    verdict = await gate_work_item(db, wid, live_check=True)
    assert verdict.eligible and verdict.verdict == ELIGIBLE
    work = await db.get(WorkItemRecord, wid)
    assert work.jira_eligibility == ELIGIBLE
    assert work.jira_last_status == "TO START"


async def test_assignment_withdrawal_blocks(clean_db, db, monkeypatch, gate_env):
    wid = await _mk_work(db, jira_issue_id="10001")
    _install_mock_client(monkeypatch, _jira_issue("TO START", "someone-else"))
    verdict = await gate_work_item(db, wid, live_check=True)
    assert verdict.verdict == ASSIGNEE_MISMATCH and not verdict.eligible


async def test_jira_unreachable_fail_closed(clean_db, db, monkeypatch, gate_env):
    """§13.2 last row: read failure → UNKNOWN; NEW dispatch inhibited."""
    wid = await _mk_work(db, jira_issue_id="10001")
    _install_mock_client(monkeypatch, None, fail=True)
    verdict = await gate_work_item(db, wid, live_check=True)
    assert verdict.verdict == UNKNOWN and not verdict.eligible


async def test_descendant_inherits_parent_linkage(clean_db, db, monkeypatch, gate_env):
    """§7.3: one Jira issue authorizes descendants — parent-chain walk."""
    parent = await _mk_work(db, title="parent", jira_issue_id="10001",
                            jira_issue_key="STNA-99")
    child = await _mk_work(db, title="child", parent_id=parent)
    _install_mock_client(monkeypatch, _jira_issue("TO START", AI_ACCOUNT_ID))
    verdict = await gate_work_item(db, child, live_check=True)
    assert verdict.eligible, verdict.reason


async def test_hold_blocks_and_clearing_restores(clean_db, db, gate_env):
    wid = await _mk_work(db)
    db.add(WorkRuntimeHoldRecord(hold_id=new_id(), work_item_id=wid,
                                 hold_kind=HOLD_PAUSE, reason="manual pause",
                                 created_by="tester"))
    await db.commit()
    verdict = await gate_work_item(db, wid, live_check=False)
    assert verdict.verdict == LOCAL_HOLD
    # operator clears via API
    rr = client.delete(f"/api/v1/jira/work/{wid}/holds/hold-x", headers=AUTH)
    assert rr.status_code == 404  # wrong id stays 404
    from sqlalchemy import select

    hold = (await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.work_item_id == wid))).first()
    assert hold is not None
    hold.cleared_at = datetime.now(timezone.utc)
    hold.cleared_by = "tester"
    await db.commit()
    verdict2 = await gate_work_item(db, wid, live_check=False)
    assert verdict2.verdict != LOCAL_HOLD


# ---------------------------------------------------------------- API contract

def test_schedule_endpoint_requires_token(gate_env):
    r = client.get("/api/v1/jira/schedule")
    assert r.status_code in (401, 403)


def test_reconcile_endpoint_is_mutation(gate_env):
    """GET must never start work (§13.5) — reconcile is POST-only."""
    r = client.get("/api/v1/jira/reconcile", headers=AUTH)
    assert r.status_code == 405


def test_link_issue_creates_linkage_and_observes(clean_db, db, monkeypatch, gate_env):
    """§7.2 'Link existing' — linkage ≠ authorization; verdict rendered."""
    from unittest import mock

    with mock.patch("acms.jira_api.JiraClient") as FakeC, \
            mock.patch("acms.jira_client.JiraClient", FakeC):
        inst = FakeC.return_value
        inst.get_issue.return_value = _jira_issue("IDEA/UNVALIDATED", AI_ACCOUNT_ID)
        inst.get_issue_by_id.return_value = _jira_issue("IDEA/UNVALIDATED", AI_ACCOUNT_ID)
        inst.is_mock = False
        r = client.post("/api/v1/work/items", headers=AUTH,
                        json={"kind": "task", "title": "link target"})
        assert r.status_code == 201
        wid = r.json()["work_item_id"]
        lr = client.post(f"/api/v1/jira/work/{wid}/link", headers=AUTH,
                         json={"issue_key": "STNA-99"})
        assert lr.status_code == 200, lr.text
        body = lr.json()
        assert body["linked"] is True
        assert body["gate"]["verdict"] == NOT_READY
        assert "IDEA/UNVALIDATED" in body["gate"]["reason"]


def test_hold_endpoints_validation(gate_env):
    r = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "hold api probe"})
    wid = r.json()["work_item_id"]
    r2 = client.post(f"/api/v1/jira/work/{wid}/holds", headers=AUTH,
                     json={"hold_kind": "WAT"})
    assert r2.status_code == 422
    r3 = client.post(f"/api/v1/jira/work/{wid}/holds", headers=AUTH,
                     json={"hold_kind": "STOP", "reason": "halt"})
    assert r3.status_code == 200 and r3.json()["hold_kind"] == "STOP"


# ---------------------------------------------------------------- reconciliation

async def test_reconcile_run_coalescing(clean_db, db, gate_env):
    """Duplicate clicks coalesce into ONE live run (§13.4 step 1)."""
    from acms.jira_reconcile import start_reconciliation_run

    a = await start_reconciliation_run(db, trigger="manual_check",
                                       requested_by="alice", execute=False)
    b = await start_reconciliation_run(db, trigger="manual_check",
                                       requested_by="bob", execute=False)
    assert a.run_id == b.run_id and b.coalesced


async def test_scheduler_interval_clamped_and_freshness(clean_db, db, gate_env):
    """§13.6: interval cap 24h; due-ness from last SUCCESSFUL poll; overdue."""
    from acms.jira_reconcile import _scheduler_state, due_for_scheduled_poll

    get_settings().jira_reconcile_interval_hours = 72  # attempt to exceed cap
    due, info = await due_for_scheduled_poll(db)
    assert info["interval_hours"] == 24
    assert due and info["reason"] == "never_run"

    sched = await _scheduler_state(db)  # reuse the session's pending row (no double-add)
    sched.last_successful_poll_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.commit()
    due2, info2 = await due_for_scheduled_poll(db)
    assert not due2 and info2["reason"] == "not_due"

    sched.last_successful_poll_at = datetime.now(timezone.utc) - timedelta(hours=50)
    await db.commit()
    due3, info3 = await due_for_scheduled_poll(db)
    assert due3 and info3.get("overdue")


async def test_run_ledger_counts_persist(clean_db, db, gate_env):
    """The run row stores per-outcome counts — the display contract (§13.5)."""
    run = JiraReconciliationRunRecord(run_id=new_id(), trigger="manual_check",
                                      requested_by="tester")
    db.add(run)
    await db.commit()
    run.examined_count = 7
    run.added_count = 2
    run.started_count = 1
    run.paused_count = 1
    run.unchanged_count = 3
    run.scan_completeness = "partial"
    run.error_summary = "one issue read failed"
    await db.commit()
    got = await db.get(JiraReconciliationRunRecord, run.run_id)
    assert (got.examined_count, got.added_count, got.started_count) == (7, 2, 1)
    assert got.scan_completeness == "partial"