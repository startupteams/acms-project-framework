import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgentCapabilities, AgentRecord, AgentRegistrationRequest, AgentResponse


def _to_response(record: AgentRecord) -> AgentResponse:
    return AgentResponse(
        agent_id=record.agent_id,
        external_registration_id=record.external_registration_id,
        display_name=record.display_name,
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


def register_agent(db: Session, request: AgentRegistrationRequest) -> tuple[AgentResponse, bool]:
    existing = db.scalar(
        select(AgentRecord).where(AgentRecord.external_registration_id == request.external_registration_id)
    )
    now = AgentRecord.now()
    capabilities_json = request.capabilities.model_dump_json()

    if existing:
        existing.display_name = request.display_name
        existing.trust_class = request.trust_class.value
        existing.harness = request.harness
        existing.bridge_version = request.bridge_version
        existing.protocol_version = request.protocol_version
        existing.card_url = str(request.card_url) if request.card_url else None
        existing.capability_hash = request.capability_hash
        existing.capability_json = capabilities_json
        existing.updated_at = now
        db.commit()
        db.refresh(existing)
        return _to_response(existing), False

    record = AgentRecord(
        agent_id=AgentRecord.new_id(),
        external_registration_id=request.external_registration_id,
        display_name=request.display_name,
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
    db.commit()
    db.refresh(record)
    return _to_response(record), True


def list_agents(db: Session) -> list[AgentResponse]:
    return [_to_response(x) for x in db.scalars(select(AgentRecord).order_by(AgentRecord.created_at)).all()]
