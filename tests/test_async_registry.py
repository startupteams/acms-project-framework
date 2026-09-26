"""Async registry persistence exercised directly against SQLAlchemy AsyncSession."""

import uuid

from acms.db import SessionLocal, engine
from acms.models import AgentRegistrationRequest, TrustClass
from acms.registry import list_agents, register_agent


async def test_register_update_and_list_via_async_session():
    external_id = f"async-unit-{uuid.uuid4()}"
    request = AgentRegistrationRequest(
        external_registration_id=external_id,
        display_name="Async Unit Agent",
        trust_class=TrustClass.INTERNAL,
        harness="hermes",
        bridge_version="0.1",
        protocol_version="a2a",
    )

    async with SessionLocal() as session:
        agent, created = await register_agent(session, request)
        assert created is True

    async with SessionLocal() as session:
        request.bridge_version = "0.9"
        agent2, created2 = await register_agent(session, request)
        assert created2 is False
        assert agent2.agent_id == agent.agent_id
        assert agent2.bridge_version == "0.9"

    async with SessionLocal() as session:
        agents = await list_agents(session)
    assert any(a.agent_id == agent.agent_id for a in agents)

    await engine.dispose()
