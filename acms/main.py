from fastapi import Depends, FastAPI, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import __version__
from .db import get_session
from .models import AgentRegistrationRequest, AgentResponse
from .registry import list_agents, register_agent
from .security import require_admin_token

app = FastAPI(title="AgentifyMe Cloud Management System", version=__version__)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str]:
    return {"version": __version__}


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
