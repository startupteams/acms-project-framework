from fastapi.testclient import TestClient

from acms.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def test_health_and_version():
    assert client.get("/health").json() == {"status": "ok"}
    assert "version" in client.get("/version").json()


def test_registry_requires_auth():
    assert client.get("/api/v1/agents").status_code in (401, 403, 503)


def test_register_reregister_preserves_acms_identity():
    payload = {
        "external_registration_id": "miam-00111-hermes-01",
        "display_name": "Hermes Worker 01",
        "trust_class": "internal",
        "harness": "hermes",
        "bridge_version": "0.1",
        "protocol_version": "a2a",
        "capabilities": {"streaming": True, "steering": True},
    }
    first = client.post("/api/v1/agents/register", headers=AUTH, json=payload)
    assert first.status_code == 201
    first_id = first.json()["agent_id"]

    payload["bridge_version"] = "0.2"
    second = client.post("/api/v1/agents/register", headers=AUTH, json=payload)
    assert second.status_code == 200
    assert second.json()["agent_id"] == first_id
    assert second.json()["bridge_version"] == "0.2"


def test_capabilities_are_explicit():
    agents = client.get("/api/v1/agents", headers=AUTH)
    assert agents.status_code == 200
    match = [a for a in agents.json() if a["external_registration_id"] == "miam-00111-hermes-01"]
    assert match, "registered agent missing from list"
    assert match[0]["capabilities"]["pause"] is False
    assert match[0]["capabilities"]["streaming"] is True
