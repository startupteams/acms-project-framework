from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import __version__
from .build_info import as_dict as build_identity
from .db import get_session
from .models import AgentRegistrationRequest, AgentResponse
from .registry import list_agents, register_agent
from .security import require_admin_token
from .ui.routes import install_ui
from .work_api import router as work_router
from .telemetry_api import router as telemetry_router
from .telemetry_scheduler import TelemetryScheduler

_scheduler = TelemetryScheduler()


@asynccontextmanager
async def lifespan(app):
    _scheduler.start()
    try:
        yield
    finally:
        await _scheduler.stop()


app = FastAPI(title="AgentifyMe Cloud Management System", version=__version__, lifespan=lifespan)

install_ui(app)
app.include_router(work_router)
app.include_router(telemetry_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str]:
    return build_identity()


@app.post("/api/v1/agents/register", response_model=AgentResponse)
async def register(
    payload: AgentRegistrationRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> AgentResponse:
    agent, created = await register_agent(db, payload)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return agent


@app.get("/api/v1/agents", response_model=list[AgentResponse])
async def agents(
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> list[AgentResponse]:
    return await list_agents(db)
