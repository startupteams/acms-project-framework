import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AgentCapabilities,
    AgentDisplayNamePatch,
    AgentRecord,
    AgentRegistrationRequest,
    AgentResponse,
)


def _to_response(record: AgentRecord) -> AgentResponse:
    return AgentResponse(
        agent_id=record.agent_id,
        external_registration_id=record.external_registration_id,
        display_name=record.display_name,
        legacy_name=record.legacy_name,
        worker_uid=record.worker_uid,
        trust_class=record.trust_class,
        harness=record.harness,
        bridge_version=record.bridge_version,
        protocol_version=record.protocol_version,
        card_url=record.card_url,
        capability_hash=record.capability_hash,
        capabilities=AgentCapabilities.model_validate(json.loads(record.capability_json)),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def register_agent(db: AsyncSession, request: AgentRegistrationRequest) -> tuple[AgentResponse, bool]:
    existing = await db.scalar(
        select(AgentRecord).where(AgentRecord.external_registration_id == request.external_registration_id)
    )
    now = AgentRecord.now()
    capabilities_json = request.capabilities.model_dump_json()

    if existing:
        existing.display_name = request.display_name
        if request.legacy_name is not None:
            existing.legacy_name = request.legacy_name
        if request.worker_uid is not None:
            existing.worker_uid = request.worker_uid
        existing.trust_class = request.trust_class.value
        existing.harness = request.harness
        existing.bridge_version = request.bridge_version
        existing.protocol_version = request.protocol_version
        existing.card_url = str(request.card_url) if request.card_url else None
        existing.capability_hash = request.capability_hash
        existing.capability_json = capabilities_json
        existing.updated_at = now
        await db.commit()
        await db.refresh(existing)
        return _to_response(existing), False

    record = AgentRecord(
        agent_id=AgentRecord.new_id(),
        external_registration_id=request.external_registration_id,
        display_name=request.display_name,
        legacy_name=request.legacy_name,
        worker_uid=request.worker_uid,
        trust_class=request.trust_class.value,
        harness=request.harness,
        bridge_version=request.bridge_version,
        protocol_version=request.protocol_version,
        card_url=str(request.card_url) if request.card_url else None,
        capability_hash=request.capability_hash,
        capability_json=capabilities_json,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _to_response(record), True


async def list_agents(db: AsyncSession) -> list[AgentResponse]:
    result = await db.scalars(select(AgentRecord).order_by(AgentRecord.created_at))
    return [_to_response(x) for x in result.all()]


async def patch_agent_display(db: AsyncSession, agent_id: str, patch: AgentDisplayNamePatch) -> AgentResponse | None:
    """Update display metadata ONLY (REQ-001: persistent identity is the
    agent_id/UUID; it never changes). Returns None when the agent is unknown."""
    record = await db.get(AgentRecord, agent_id)
    if record is None:
        return None
    record.display_name = patch.display_name
    record.legacy_name = patch.legacy_name
    record.worker_uid = patch.worker_uid
    record.updated_at = AgentRecord.now()
    await db.commit()
    await db.refresh(record)
    return _to_response(record)
