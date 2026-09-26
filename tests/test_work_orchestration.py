"""Phase 3 work orchestration tests (ACMS-REQ-007/008/013/014/036).

Exercises the service-layer invariants (one ACTIVE primary per agent, work-item
hierarchy, routine inventory, execution-task traceability) and the HTTP API
surface defined in acms/work_api.py, using the shared SQLite unit-test database
from conftest.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acms.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def _register_agent(external_id: str) -> str:
    resp = client.post(
        "/api/v1/agents/register",
        headers=AUTH,
        json={
            "external_registration_id": external_id,
            "display_name": "Work Test Agent",
            "trust_class": "internal",
            "harness": "hermes",
            "bridge_version": "0.1",
            "protocol_version": "a2a",
            "capabilities": {"streaming": True},
        },
    )
    assert resp.status_code in (200, 201)
    return resp.json()["agent_id"]


def _create_item(title: str, parent_id: str | None = None, kind: str = "task"):
    return client.post(
        "/api/v1/work/items",
        headers=AUTH,
        json={"kind": kind, "parent_id": parent_id, "title": title, "scope_markdown": "# scope"},
    )


# ---------------------------------------------------------------- work items


def test_create_get_list_work_item():
    created = client.post(
        "/api/v1/work/items",
        headers=AUTH,
        json={"kind": "project", "title": "ACMS Product", "scope_markdown": "# scope"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "project"
    assert body["status"] == "planned"

    # Child traces to parent (ACMS-REQ-007)
    child = client.post(
        "/api/v1/work/items",
        headers=AUTH,
        json={"kind": "feature", "parent_id": body["work_item_id"], "title": "child feature"},
    )
    assert child.status_code == 201
    assert child.json()["parent_id"] == body["work_item_id"]

    # Unknown parent rejected
    missing = client.post(
        "/api/v1/work/items",
        headers=AUTH,
        json={"title": "orphan", "parent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing.status_code == 404

    got = client.get(f"/api/v1/work/items/{body['work_item_id']}", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["title"] == "ACMS Product"

    listed = client.get("/api/v1/work/items", headers=AUTH)
    assert listed.status_code == 200
    assert any(i["work_item_id"] == body["work_item_id"] for i in listed.json())


def test_patch_work_item_status():
    item_id = client.post(
        "/api/v1/work/items", headers=AUTH, json={"title": "Status item"}
    ).json()["work_item_id"]
    patched = client.patch(
        f"/api/v1/work/items/{item_id}",
        headers=AUTH,
        json={"status": "blocked"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "blocked"


# ---------------------------------------------------------------- assignments (REQ-008)


def test_one_primary_assignment_invariant():
    agent_id = _register_agent("work-inv-01")
    item_a = _create_item("Work A").json()["work_item_id"]
    item_b = client.post(
        "/api/v1/work/items", headers=AUTH, json={"title": "Other work"}
    ).json()["work_item_id"]

    first = client.post(
        "/api/v1/work/assignments",
        headers=AUTH,
        json={"agent_id": agent_id, "work_item_id": item_a},
    )
    assert first.status_code == 201
    assert first.json()["status"] == "ACTIVE"

    # Second primary while first ACTIVE -> 409 (ACMS-REQ-008)
    busy = client.post(
        "/api/v1/work/assignments",
        headers=AUTH,
        json={"agent_id": agent_id, "work_item_id": item_b},
    )
    assert busy.status_code == 409
    assert busy.json()["detail"] == "agent-has-active-assignment"

    # Complete the first, then a new assignment succeeds.
    close = client.post(
        f"/api/v1/work/assignments/{first.json()['assignment_id']}/close?new_status=COMPLETED",
        headers=AUTH,
    )
    assert close.status_code == 200
    assert close.json()["status"] == "COMPLETED"

    second = client.post(
        "/api/v1/work/assignments",
        headers=AUTH,
        json={"agent_id": agent_id, "work_item_id": item_b},
    )
    assert second.status_code == 201

    # Work item was activated on assignment (PLANNED -> ACTIVE).
    item = client.get(f"/api/v1/work/items/{item_a}", headers=AUTH).json()
    assert item["status"] == "active"


def test_assign_requires_known_agent_and_item():
    resp = client.post(
        "/api/v1/work/assignments",
        headers=AUTH,
        json={"agent_id": "00000000-0000-0000-0000-000000000000", "work_item_id": "nope"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- execution tasks (REQ-036)


def test_execution_task_traceability():
    agent_id = _register_agent("work-tasks-01")
    item_id = client.post(
        "/api/v1/work/items", headers=AUTH, json={"title": "Item with runs"}
    ).json()["work_item_id"]

    # Multiple execution tasks trace to the same work item without new work items.
    t1 = client.post(
        "/api/v1/work/tasks",
        headers=AUTH,
        json={"work_item_id": item_id, "agent_id": agent_id, "external_task_id": "a2a-run-1"},
    )
    assert t1.status_code == 201
    t2 = client.post(
        "/api/v1/work/tasks",
        headers=AUTH,
        json={"work_item_id": item_id, "agent_id": agent_id, "external_task_id": "retry-2"},
    )
    assert t2.status_code == 201

    listed = client.get(f"/api/v1/work/tasks?work_item_id={item_id}", headers=AUTH)
    assert listed.status_code == 200
    assert {r["task_id"] for r in listed.json()} == {
        t1.json()["task_id"],
        t2.json()["task_id"],
    }

    done = client.post(
        f"/api/v1/work/tasks/{t1.json()['task_id']}/finish?new_status=SUCCEEDED", headers=AUTH
    )
    assert done.status_code == 200
    assert done.json()["status"] == "SUCCEEDED"
    assert done.json()["finished_at"] is not None
    assert t2.json()["status"] == "RUNNING"

    # Unknown work item rejected
    missing = client.post(
        "/api/v1/work/tasks", headers=AUTH, json={"work_item_id": "does-not-exist"}
    )
    assert missing.status_code == 404


# ---------------------------------------------------------------- routines (REQ-014)


def test_background_routine_inventory_upsert():
    agent_id = _register_agent("work-routines-01")

    first = client.put(
        f"/api/v1/work/agents/{agent_id}/routines",
        headers=AUTH,
        json={
            "name": "nightly-export",
            "purpose": "sync artifacts",
            "schedule": "0 3 * * *",
            "enabled": True,
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert body["enabled"] is True
    assert body["latest_status"] is None

    # Reporting a status on the same named routine updates the row in place.
    report = client.put(
        f"/api/v1/work/agents/{agent_id}/routines",
        headers=AUTH,
        json={"name": "nightly-export", "enabled": True, "latest_status": "ran ok"},
    )
    assert report.status_code == 200
    assert report.json()["latest_status"] == "ran ok"
    assert report.json()["routine_id"] == body["routine_id"]

    listed = client.get("/api/v1/work/routines", headers=AUTH).json()
    rows = [r for r in listed if r["agent_id"] == agent_id]
    assert len([r for r in listed if r["agent_id"] == agent_id]) == 1


def test_routine_requires_known_agent():
    resp = client.put(
        "/api/v1/work/agents/00000000-0000-0000-0000-000000000000/routines",
        headers=AUTH,
        json={"name": "orphan-routine"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- handoffs (REQ-013)


def test_handoff_create_and_list():
    item_id = client.post(
        "/api/v1/work/items", headers=AUTH, json={"title": "Handoff item"}
    ).json()["work_item_id"]
    created = client.post(
        "/api/v1/work/handoffs",
        headers=AUTH,
        json={
            "work_item_id": item_id,
            "title": "Handoff for handoff item",
            "body_markdown": "## Observed\n- change A\n\n## Interpretation\n- looks good",
        },
    )
    listed = client.get(f"/api/v1/work/handoffs?work_item_id={item_id}", headers=AUTH)
    assert listed.status_code == 200
    titles = [h["title"] for h in listed.json()]
    assert "Handoff for handoff item" in titles

    # A second handoff on the same item is listed too.
    second = client.post(
        "/api/v1/work/handoffs",
        headers=AUTH,
        json={"work_item_id": item_id, "title": "Second handoff", "body_markdown": "x"},
    )
    assert second.status_code == 201
    titles_after = [h["title"] for h in client.get(
        f"/api/v1/work/handoffs?work_item_id={item_id}", headers=AUTH
    ).json()]
    assert "Handoff for handoff item" in titles_after
    assert "Second handoff" in titles_after
    assert len(titles_after) == 2


# ---------------------------------------------------------------- auth


def test_unauthenticated_work_api_denied():
    assert client.get("/api/v1/work/items", headers={}).status_code in (401, 403)
    assert client.post("/api/v1/work/items", headers={}, json={"title": "x"}).status_code in (401, 403)
    assert client.get("/api/v1/work/assignments", headers={}).status_code in (401, 403)
