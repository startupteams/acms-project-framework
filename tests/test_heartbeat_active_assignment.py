"""Regression (live-found 2026-10-01): heartbeat ingest crashed with
AttributeError 'AssignmentRecord' object has no attribute 'work_key' for ANY
agent holding an ACTIVE primary assignment — the poller then silently dropped
that agent's heartbeats (worker-001 absent from agent_status_current while
002-005 ingested fine). The work key lives on the WORK ITEM, not the
assignment row. ACMS-REQ-033/053 trace."""
from __future__ import annotations

from .test_live_telemetry import AUTH, AGENT, _hb, _post_hb, client


def test_heartbeat_ingests_with_active_assignment(clean_db):
    # agent + work item + ACTIVE assignment (the crash precondition)
    r = client.post("/api/v1/agents/register", headers=AUTH, json=AGENT)
    aid = r.json()["agent_id"]
    wi = client.post("/api/v1/work/items", headers=AUTH, json={
        "title": "heartbeat regression item", "created_by": "test"}).json()
    asg = client.post("/api/v1/work/assignments", headers=AUTH, json={
        "agent_id": aid, "work_item_id": wi["work_item_id"]}).json()
    assert asg["status"] == "ACTIVE"

    # heartbeat ingest must NOT 500 (it did before the fix)
    r = _post_hb(aid, _hb(aid))
    assert r.status_code == 200, r.text

    st = client.get(f"/api/v1/fleet/agents/{aid}/status", headers=AUTH).json()
    assert st["connectivity"] == "HEALTHY"
    # expected work key derived from the assignment's WORK ITEM (allocated
    # ACMS-WORK-#### key; the item response itself omits work_key)
    wid = client.get(f"/api/v1/work/items/{wi['work_item_id']}", headers=AUTH)
    assert wid.status_code == 200
    assert st.get("expected_work_key") and st["expected_work_key"].startswith("ACMS-WORK-")
    assert st.get("expected_assignment_key") and st["expected_assignment_key"].startswith("ACMS-ASG-")


def test_heartbeat_no_assignment_still_healthy(clean_db):
    r = client.post(
        "/api/v1/agents/register", headers=AUTH,
        json={**AGENT, "external_registration_id": "telemetry-regression-02"})
    aid = r.json()["agent_id"]
    r = _post_hb(aid, _hb(aid))
    assert r.status_code == 200, r.text
    st = client.get(f"/api/v1/fleet/agents/{aid}/status", headers=AUTH).json()
    assert st["connectivity"] == "HEALTHY"
    assert st.get("expected_work_key") is None
