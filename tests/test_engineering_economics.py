"""Engineering-economics tests (REV2 plan §9B).

Covers: session→PR correlation, session→requirement correlation, one PR→
several requirements, several PRs→one requirement, failed-run costs included,
unknown acceptance stays unknown, unknown human time stays unknown, no double
counting in cost rollup, model/task filtering.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch, clean_db):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


def _mk_outcome(client, **over) -> str:
    body = {"repository": "startupteams/acms-project-framework",
            "branch": "feat/x", "pr_number": 30,
            "model_id": "glm-5.3-flash", "provider": "openrouter",
            "task_category": "backend"}
    body.update(over)
    r = client.post("/api/v1/economics/outcomes", headers=AUTH, json=body)
    assert r.status_code == 201, r.text
    return r.json()["outcome_id"]


def _mk_session(client, cost=None, model="glm-5.3-flash", task_category=None) -> str:
    r = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": "00000000-0000-0000-0000-00000000ccc1",
        "model_id": model,
        **({"task_category": task_category} if task_category else {}),
    })
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    if cost is not None:
        client.post(f"/api/v1/memory/sessions/{sid}/telemetry", headers=AUTH,
                    json={"estimated_cost_usd": cost})
    return sid


def test_outcome_lifecycle_and_acceptance_rules(client):
    oid = _mk_outcome(client, pr_number=31)
    # MERGED alone never implies ACCEPTED
    r = client.patch(f"/api/v1/economics/outcomes/{oid}", headers=AUTH,
                     json={"outcome_state": "MERGED"})
    assert r.json()["outcome_state"] == "MERGED"
    rep = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH).json()
    assert rep["outcomes"]["accepted"] == 0
    assert rep["cost_per_accepted_pr"] is None  # unknown, not zero
    # ACCEPTED requires accepted_by
    r = client.patch(f"/api/v1/economics/outcomes/{oid}", headers=AUTH,
                     json={"outcome_state": "ACCEPTED"})
    assert r.status_code == 422
    r = client.patch(f"/api/v1/economics/outcomes/{oid}", headers=AUTH,
                     json={"outcome_state": "ACCEPTED", "accepted_by": "jordatech"})
    assert r.json()["outcome_state"] == "ACCEPTED"
    assert r.json()["accepted_by"] == "jordatech"
    assert r.json()["accepted_at"] is not None


def test_many_to_many_requirement_links(client):
    # one PR → several requirements
    oid = _mk_outcome(client, pr_number=32)
    r = client.post(f"/api/v1/economics/outcomes/{oid}/requirements", headers=AUTH,
                    json={"requirement_refs": ["ACMS-REQ-059", "ACMS-REQ-062"]})
    assert sorted(r.json()["requirement_refs"]) == ["ACMS-REQ-059", "ACMS-REQ-062"]
    # idempotent re-link
    r2 = client.post(f"/api/v1/economics/outcomes/{oid}/requirements", headers=AUTH,
                     json={"requirement_refs": ["ACMS-REQ-059"]})
    assert len(r2.json()["requirement_refs"]) == 2
    # several PRs → one requirement
    oid_b = _mk_outcome(client, pr_number=33)
    client.post(f"/api/v1/economics/outcomes/{oid_b}/requirements", headers=AUTH,
                json={"requirement_refs": ["ACMS-REQ-059"]})
    client.patch(f"/api/v1/economics/outcomes/{oid}", headers=AUTH,
                 json={"outcome_state": "ACCEPTED", "accepted_by": "jordatech"})
    client.patch(f"/api/v1/economics/outcomes/{oid_b}", headers=AUTH,
                 json={"outcome_state": "ACCEPTED", "accepted_by": "jordatech"})
    rep = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH).json()
    assert rep["requirements"]["linked_distinct"] == 2
    assert rep["requirements"]["accepted_distinct"] == 2  # REQ-059 counted once


def test_failed_run_cost_included_and_no_double_count(client):
    oid = _mk_outcome(client, pr_number=34)
    # successful attempt $2 + failed attempt $1 → both count
    client.post(f"/api/v1/economics/outcomes/{oid}/costs", headers=AUTH,
                json={"api_cost_usd": 2.0, "model_id": "glm-5.3-flash"})
    client.post(f"/api/v1/economics/outcomes/{oid}/costs", headers=AUTH,
                json={"api_cost_usd": 1.0, "model_id": "glm-5.3-flash",
                      "failed_run": True})
    rep = client.get("/api/v1/economics/reports/model-comparison",
                     headers=AUTH, params={"model_id": "glm-5.3-flash"}).json()
    assert abs(rep["total_cost_usd"] - 3.0) < 1e-9
    assert abs(rep["failed_run_cost_usd"] - 1.0) < 1e-9
    # attach a session (source=session) — session cost joins, still no double count
    sid = _mk_session(client, cost=0.5)
    r = client.post(f"/api/v1/economics/outcomes/{oid}/costs", headers=AUTH,
                    json={"session_id": sid})
    assert r.status_code == 201
    rep2 = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH).json()
    assert abs(rep2["total_cost_usd"] - 3.5) < 1e-9


def test_unknown_cost_and_human_time_stay_unknown(client):
    oid = _mk_outcome(client, pr_number=35)
    # cost entry with NO dollars (tokens only)
    client.post(f"/api/v1/economics/outcomes/{oid}/costs", headers=AUTH,
                json={"input_tokens": 1000, "output_tokens": 200})
    client.patch(f"/api/v1/economics/outcomes/{oid}", headers=AUTH,
                 json={"outcome_state": "ACCEPTED", "accepted_by": "jordatech"})
    rep = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH).json()
    assert rep["entries_with_unknown_cost"] == 1
    assert rep["total_cost_usd"] == 0.0
    assert rep["human_time_unknown_count"] == 1
    assert rep["human_review_minutes"] is None  # never zero-filled
    # once recorded, human time sums
    oid2 = _mk_outcome(client, pr_number=36)
    client.patch(f"/api/v1/economics/outcomes/{oid2}", headers=AUTH,
                 json={"outcome_state": "ACCEPTED", "accepted_by": "jordatech",
                       "human_review_minutes": 12.0})
    rep2 = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH).json()
    assert rep2["human_review_minutes"] == 12.0


def test_model_and_task_filtering(client):
    glm = _mk_outcome(client, pr_number=37, model_id="glm-5.3-flash",
                      task_category="backend")
    other = _mk_outcome(client, pr_number=38, model_id="other-model",
                        task_category="infrastructure")
    client.post(f"/api/v1/economics/outcomes/{glm}/costs", headers=AUTH,
                json={"api_cost_usd": 4.0, "model_id": "glm-5.3-flash"})
    client.post(f"/api/v1/economics/outcomes/{other}/costs", headers=AUTH,
                json={"api_cost_usd": 9.0, "model_id": "other-model"})
    for oid in (glm, other):
        client.patch(f"/api/v1/economics/outcomes/{oid}", headers=AUTH,
                     json={"outcome_state": "ACCEPTED", "accepted_by": "jordatech"})
    by_model = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH,
                          params={"model_id": "glm-5.3-flash"}).json()
    assert by_model["outcomes"]["accepted"] == 1
    assert abs(by_model["total_cost_usd"] - 4.0) < 1e-9
    assert abs(by_model["cost_per_accepted_pr"] - 4.0) < 1e-9
    by_task = client.get("/api/v1/economics/reports/model-comparison", headers=AUTH,
                         params={"task_category": "infrastructure"}).json()
    assert by_task["outcomes"]["accepted"] == 1
    assert abs(by_task["total_cost_usd"] - 9.0) < 1e-9


def test_session_task_category_recorded(client):
    sid = _mk_session(client, cost=0.1, task_category="debugging")
    s = client.get(f"/api/v1/memory/sessions/{sid}", headers=AUTH).json()
    assert s["task_category"] == "debugging"


def test_economics_requires_token(client):
    assert client.get("/api/v1/economics/reports/model-comparison").status_code == 401


def test_invalid_task_category_rejected(client):
    r = client.post("/api/v1/economics/outcomes", headers=AUTH,
                    json={"task_category": "banana"})
    assert r.status_code == 422
