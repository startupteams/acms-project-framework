"""Server Manager integration (JINT-001, REV4 §12/§13/§14).

Persistent-identity reservation + provisioning request tracking.

Create Agent workflow:
1. ACMS Administrator (or the pre-authorized sprint execution context)
   submits a Create-Agent request -> ACMS reserves the persistent identity
   (an ``agents`` row with a reserved external_registration_id) -> a
   ``provisioning_requests`` row records the approval authority (§14).
2. ACMS calls Server Manager (runtime:write scope) to provision the
   runtime; the job id + runtime id are correlated here.
3. Status polling goes through :mod:`acms.server_manager_client`.

ACMS never touches Proxmox/VMID details beyond optional diagnostics (§13).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, get_session
from .models import AgentRecord, TrustClass
from .security import require_admin_token
from .server_manager_client import (
    ServerManagerClient,
    ServerManagerError,
    ServerManagerForbidden,
)
from .settings import get_settings

router = APIRouter(prefix="/api/v1/server-manager", tags=["server-manager"])


# ------------------------------------------------------------------ models
class ProvisioningRequest(Base):
    __tablename__ = "provisioning_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # idempotency key to SM
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(30), default="APPROVED")
    # RESERVED → APPROVED → PROVISIONING → LIVE / FAILED / RETIRED
    authority: Mapped[str] = mapped_column(String(60))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    approver: Mapped[str] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc))


class ProvisioningRequestRead(BaseModel):
    id: str
    agent_id: str
    request_id: str
    job_id: str | None
    runtime_id: str | None
    state: str
    authority: str
    approver: str
    error: str | None


class CreateAgentRequest(BaseModel):
    display_name: str = Field(min_length=3, max_length=120)
    harness: str = "hermes"
    runtime_class: str = "software_development_worker"
    model_route: str | None = None
    trust_class: TrustClass = TrustClass.INTERNAL
    authority: str = Field(default="human_in_acms")
    approver: str = Field(min_length=2, max_length=120)
    request_id: str | None = None


class CreateAgentResponse(BaseModel):
    agent_id: str
    external_registration_id: str
    request: ProvisioningRequestRead


def _client() -> ServerManagerClient:
    s = get_settings()
    if not s.server_manager_base_url or not s.server_manager_token:
        raise HTTPException(status_code=503, detail="Server Manager integration not configured")
    return ServerManagerClient(s.server_manager_base_url, s.server_manager_token)


# ----------------------------------------------------------------- routes
@router.post("/agents", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin_token)])
async def create_agent(body: CreateAgentRequest, db: AsyncSession = Depends(get_session)) -> CreateAgentResponse:
    """Reserve persistent identity + record the approved provisioning request.

    Authority (§14): the caller names where approval came from. The sprint
    execution context is a valid authority when pre-authorized (REV4 §1/§14).
    """
    request_id = body.request_id or f"acms-{uuid.uuid4()}"
    # idempotency: same request_id -> the original request
    existing = (await db.execute(
        select(ProvisioningRequest).where(ProvisioningRequest.request_id == request_id)
    )).scalar_one_or_none()
    if existing is not None:
        agent = await db.get(AgentRecord, existing.agent_id)
        return CreateAgentResponse(
            agent_id=agent.agent_id, external_registration_id=agent.external_registration_id,
            request=_read(existing))

    agent_id = AgentRecord.new_id()
    ext_reg = f"acms-reserved-{agent_id[:8]}"
    agent = AgentRecord(
        agent_id=agent_id, external_registration_id=ext_reg,
        display_name=body.display_name, trust_class=body.trust_class.value
        if hasattr(body.trust_class, "value") else str(body.trust_class),
        harness=body.harness, bridge_version="pending-bootstrap",
        protocol_version="acms-bridge-v1", capability_json="{}",
        created_at=AgentRecord.now(), updated_at=AgentRecord.now(),
    )
    db.add(agent)
    prov = ProvisioningRequest(
        id=str(uuid.uuid4()), agent_id=agent_id, request_id=request_id,
        state="APPROVED", authority=body.authority, approver=body.approver,
    )
    db.add(prov)
    await db.commit()
    await db.refresh(prov)
    return CreateAgentResponse(agent_id=agent_id, external_registration_id=ext_reg, request=_read(prov))


@router.post("/requests/{prov_id}/provision", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(require_admin_token)])
async def provision_agent(prov_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    """Call Server Manager to provision the runtime for an approved request."""
    prov = await db.get(ProvisioningRequest, prov_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="provisioning request not found")
    if prov.state not in ("APPROVED", "FAILED"):
        raise HTTPException(status_code=409, detail=f"cannot provision from state {prov.state}")
    agent = await db.get(AgentRecord, prov.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="reserved identity missing")

    client = _client()
    # Retry semantics (§13): a FAILED request re-provisions with a NEW request_id — the original
    # request_id is idempotency-bound to its original job on the Server Manager side, so a retry
    # must be a distinct attempt (attempt counter in the request id).
    attempt_request_id = prov.request_id
    if prov.state == "FAILED":
        # attempt = monotonic attempt number (1 = original). The retry marker
        # carries the RETRY COUNT (first retry → |retry:1) so the id is stable
        # per attempt: attempt N produces |retry:(N-1).
        prov.attempt = (prov.attempt or 1) + 1
        attempt_request_id = f"{prov.request_id}|retry:{prov.attempt - 1}"
    try:
        job = client.create_runtime(
            acms_agent_id=agent.agent_id, name=agent.display_name,
            harness=agent.harness, request_id=attempt_request_id,
            model_route=None, bridge_profile={"registered_by": prov.authority},
        )
    except ServerManagerForbidden as e:
        prov.state = "FAILED"
        prov.error = f"ownership/auth refusal: {e}"
        await db.commit()
        raise HTTPException(status_code=403, detail=str(e))
    except ServerManagerError as e:
        prov.state = "FAILED"
        prov.error = str(e)[:800]
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Server Manager error: {e}")

    prov.job_id = job.job_id
    # Honest state mapping (capability honesty per repo policy): if Server Manager returned
    # a terminal job state, record THAT, not a provisional PROVISIONING.
    if job.state == "FAILED":
        prov.state = "FAILED"
        prov.error = (job.error or "Server Manager job failed")[:800]
    elif job.state == "DONE":
        prov.state = "LIVE"
        # correlate runtime id now that provisioning completed synchronously
        try:
            rt = client.list_runtimes(acms_agent_id=agent.agent_id)
            if rt:
                prov.runtime_id = rt[0].runtime_id
        except ServerManagerError:
            pass
    else:
        prov.state = "PROVISIONING"
    prov.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"job_id": job.job_id, "state": job.state, "request_id": prov.request_id,
            "request_state": prov.state}


@router.get("/requests/{prov_id}", dependencies=[Depends(require_admin_token)])
async def get_request(prov_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    prov = await db.get(ProvisioningRequest, prov_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="not found")
    out = _read(prov).model_dump()
    # enrich: live job/runtime state from Server Manager (optional diagnostics §13)
    if prov.job_id:
        try:
            client = _client()
            job = client.get_job(prov.job_id)
            out["job_state"] = job.state
            if prov.runtime_id:
                rt = client.get_runtime(prov.runtime_id)
                out["runtime_actual_state"] = rt.actual_state
                out["hermes_state_branch"] = rt.hermes_state_branch
        except ServerManagerError as e:
            out["job_state"] = f"unreachable: {e.__class__.__name__}"
    return out


def _read(p: ProvisioningRequest) -> ProvisioningRequestRead:
    return ProvisioningRequestRead(
        id=p.id, agent_id=p.agent_id, request_id=p.request_id, job_id=p.job_id,
        runtime_id=p.runtime_id, state=p.state, authority=p.authority,
        approver=p.approver, error=p.error)