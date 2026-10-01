"""Durable-UID naming (STEA-004 §9): legacy_name / worker_uid + display-name PATCH.

REQ-001 trace: agent_id UUID is THE persistent identity; the naming work must
preserve it. Migration/model parity: test_migration_model_parity covers agents
via models.py import (no new table here — additive columns only).
"""
from __future__ import annotations

import pytest

from acms.db import get_session
from acms.main import app
from acms.models import (
    AgentDisplayNamePatch,
    AgentRegistrationRequest,
    AgentResponse,
    TrustClass,
)
from acms.registry import patch_agent_display, register_agent

from .conftest import clean_db  # noqa: F401  (fixture import)


def _reg(name: str = "acms-worker-002", uid: str | None = None) -> AgentRegistrationRequest:
    return AgentRegistrationRequest(
        external_registration_id=f"arm-{name}",
        display_name=name,
        legacy_name=name if uid else None,
        worker_uid=uid,
        trust_class=TrustClass.INTERNAL,
        harness="hermes",
        bridge_version="0.19.0",
        protocol_version="1",
    )


async def _seed(name: str = "acms-worker-002", uid: str | None = None) -> AgentResponse:
    gen = get_session()
    db = await anext(gen)
    try:
        agent, _ = await register_agent(db, _reg(name, uid))
        return agent
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_register_stores_uid_and_legacy_name(clean_db):
    agent = await _seed("acms-worker-002", uid="002")
    assert agent.worker_uid == "002"
    assert agent.legacy_name == "acms-worker-002"
    # UUID identity assigned and stable
    assert len(agent.agent_id) == 36


@pytest.mark.asyncio
async def test_patch_display_preserves_identity(clean_db):
    seeded = await _seed("acms-worker-002")
    original_id = seeded.agent_id
    original_ext = seeded.external_registration_id

    gen = get_session()
    db = await anext(gen)
    try:
        patched = await patch_agent_display(
            db,
            original_id,
            AgentDisplayNamePatch(
                display_name="acms-hermes-worker-uid-002",
                legacy_name="acms-worker-002",
                worker_uid="002",
            ),
        )
        assert patched is not None
        # identity fields immutable
        assert patched.agent_id == original_id
        assert patched.external_registration_id == original_ext
        # display fields updated
        assert patched.display_name == "acms-hermes-worker-uid-002"
        assert patched.legacy_name == "acms-worker-002"
        assert patched.worker_uid == "002"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_patch_unknown_agent_returns_none(clean_db):
    gen = get_session()
    db = await anext(gen)
    try:
        patched = await patch_agent_display(
            db,
            "00000000-0000-0000-0000-000000000000",
            AgentDisplayNamePatch(display_name="x"),
        )
        assert patched is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_patch_http_endpoint_404_and_happy_path(clean_db):
    """API route: 404 unknown agent; happy path renames + keeps UUID."""
    from fastapi.testclient import TestClient

    seeded = await _seed("acms-worker-005", uid="005")
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}

    r404 = client.patch(
        "/api/v1/agents/00000000-0000-0000-0000-000000000000",
        json={"display_name": "x"},
        headers=headers,
    )
    assert r404.status_code == 404

    r = client.patch(
        f"/api/v1/agents/{seeded.agent_id}",
        json={
            "display_name": "acms-hermes-worker-uid-005",
            "legacy_name": "acms-worker-005",
            "worker_uid": "005",
        },
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "acms-hermes-worker-uid-005"
    assert body["legacy_name"] == "acms-worker-005"
    assert body["worker_uid"] == "005"
    assert body["agent_id"] == seeded.agent_id

    # list endpoint reflects the rename (UI renders display_name from the same record)
    rl = client.get("/api/v1/agents", headers=headers)
    assert rl.status_code == 200
    names = [a["display_name"] for a in rl.json()]
    assert "acms-hermes-worker-uid-005" in names


@pytest.mark.asyncio
async def test_register_upsert_keeps_agent_uuid(clean_db):
    """Re-registration with the same external id must NOT mint a new UUID (REQ-001)."""
    seeded = await _seed("acms-worker-003", uid="003")
    gen = get_session()
    db = await anext(gen)
    try:
        agent2, created = await register_agent(
            db, _reg("acms-worker-003", uid="003").model_copy(
                update={"display_name": "acms-hermes-worker-uid-003"})
        )
        assert created is False
        assert agent2.agent_id == seeded.agent_id
        assert agent2.display_name == "acms-hermes-worker-uid-003"
    finally:
        await db.close()
