"""JINT-001 — ACMS Server Manager integration tests (REV4 §12/§13)."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    # point the client at a fake SM server (httpx MockTransport would need the
    # client to accept it; instead monkeypatch ServerManagerClient methods)
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-admin-token"}


def test_create_agent_requires_admin(client):
    r = client.post("/api/v1/server-manager/agents", json={})
    assert r.status_code == 401, r.text


def test_create_agent_reserves_identity_and_records_request(client):
    body = {
        "display_name": "acms-worker-001",
        "authority": "pre-authorized_sprint_execution_context",
        "approver": "jordan",
    }
    r = client.post("/api/v1/server-manager/agents", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["agent_id"] and d["external_registration_id"].startswith("acms-reserved-")
    assert d["request"]["state"] == "APPROVED"
    assert d["request"]["authority"] == "pre-authorized_sprint_execution_context"


def test_create_agent_idempotent_on_request_id(client):
    body = {
        "display_name": "acms-worker-001",
        "authority": "pre-authorized_sprint_execution_context",
        "approver": "jordan",
        "request_id": "acms-test-idem-001",
    }
    r1 = client.post("/api/v1/server-manager/agents", json=body, headers=AUTH)
    assert r1.status_code == 201
    r2 = client.post("/api/v1/server-manager/agents", json=body, headers=AUTH)
    assert r2.status_code == 201
    assert r2.json()["agent_id"] == r1.json()["agent_id"]
    assert r2.json()["request"]["id"] == r1.json()["request"]["id"]


def test_provision_unconfigured_sm_returns_503(client):
    body = {
        "display_name": "acms-worker-001",
        "authority": "pre-authorized_sprint_execution_context",
        "approver": "jordan",
    }
    r = client.post("/api/v1/server-manager/agents", json=body, headers=AUTH)
    prov_id = r.json()["request"]["id"]
    r2 = client.post(f"/api/v1/server-manager/requests/{prov_id}/provision", headers=AUTH)
    assert r2.status_code == 503, r2.text


def test_provision_calls_sm_and_records_job(client, monkeypatch):
    from acms.server_manager_client import ProvisionJob

    calls = {}

    def fake_create(self, **kwargs):
        calls.update(kwargs)
        return ProvisionJob(job_id="job-123", state="DONE", error=None, steps=[], created=True)

    from acms.server_manager_client import ServerManagerClient
    monkeypatch.setattr(ServerManagerClient, "create_runtime", fake_create)

    body = {
        "display_name": "acms-worker-001",
        "authority": "pre-authorized_sprint_execution_context",
        "approver": "jordan",
    }
    r = client.post("/api/v1/server-manager/agents", json=body, headers=AUTH)
    prov_id = r.json()["request"]["id"]
    agent_id = r.json()["agent_id"]

    # configure the SM settings
    from acms.settings import get_settings
    monkeypatch.setattr(get_settings(), "server_manager_base_url", "http://sm.test")
    monkeypatch.setattr(get_settings(), "server_manager_token", "tok")

    r2 = client.post(f"/api/v1/server-manager/requests/{prov_id}/provision", headers=AUTH)
    assert r2.status_code == 202, r2.text
    assert r2.json()["job_id"] == "job-123"
    assert calls["acms_agent_id"] == agent_id
    assert calls["request_id"].startswith("acms-")  # request idempotency propagated

    # status endpoint reflects state
    r3 = client.get(f"/api/v1/server-manager/requests/{prov_id}", headers=AUTH)
    assert r3.json()["state"] == "PROVISIONING"


def test_provision_ownership_refusal_maps_403(client, monkeypatch):
    from acms.server_manager_client import ServerManagerClient, ServerManagerForbidden

    def fake_create(self, **kwargs):
        raise ServerManagerForbidden("destroy refused: ownership marker mismatch")

    monkeypatch.setattr(ServerManagerClient, "create_runtime", fake_create)
    from acms.settings import get_settings
    monkeypatch.setattr(get_settings(), "server_manager_base_url", "http://sm.test")
    monkeypatch.setattr(get_settings(), "server_manager_token", "tok")

    body = {
        "display_name": "acms-worker-001",
        "authority": "pre-authorized_sprint_execution_context",
        "approver": "jordan",
    }
    r = client.post("/api/v1/server-manager/agents", json=body, headers=AUTH)
    prov_id = r.json()["request"]["id"]
    r2 = client.post(f"/api/v1/server-manager/requests/{prov_id}/provision", headers=AUTH)
    assert r2.status_code == 403
