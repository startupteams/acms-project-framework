from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import __version__
from .build_info import as_dict as build_identity
from .db import get_session
from .models import AgentDisplayNamePatch, AgentRegistrationRequest, AgentResponse
from .registry import list_agents, patch_agent_display, register_agent
from .security import require_admin_token
from .ui.routes import install_ui
from .work_api import router as work_router
from .telemetry_api import router as telemetry_router
from .telemetry_scheduler import TelemetryScheduler

_scheduler = TelemetryScheduler()


@asynccontextmanager
async def lifespan(app):
    _scheduler.start()
    # window-5 §13.6: durable Jira reconciliation scheduler (≤24h, UTC, catch-up)
    from .jira_scheduler import start_scheduler

    start_scheduler()
    try:
        yield
    finally:
        await _scheduler.stop()


app = FastAPI(title="AgentifyMe Cloud Management System", version=__version__, lifespan=lifespan)

install_ui(app)
app.include_router(work_router)
app.include_router(telemetry_router)

from .server_manager_api import router as server_manager_router  # noqa: E402

app.include_router(server_manager_router)

from .runtime_api import router as runtime_router  # noqa: E402

app.include_router(runtime_router)

from .memory_api import router as memory_router  # noqa: E402

app.include_router(memory_router)

from .budget_api import router as budget_router  # noqa: E402

app.include_router(budget_router)

from .attention_api import router as attention_router  # noqa: E402

app.include_router(attention_router)

from .dispatch_api import router as dispatch_router  # noqa: E402

app.include_router(dispatch_router)

from .sse_api import router as sse_router  # noqa: E402

app.include_router(sse_router)

from .economics_api import router as economics_router  # noqa: E402

app.include_router(economics_router)

from .callback_api import router as callback_router  # noqa: E402

app.include_router(callback_router)

from .jira_api import router as jira_router  # noqa: E402

app.include_router(jira_router)

from .bootstrap_api import router as bootstrap_router  # noqa: E402

app.include_router(bootstrap_router)

from .a2a_api import router as a2a_router  # noqa: E402

app.include_router(a2a_router)


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


@app.patch("/api/v1/agents/{agent_id}", response_model=AgentResponse)
async def patch_agent(
    agent_id: str,
    payload: AgentDisplayNamePatch,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin_token),
) -> AgentResponse:
    """Rename display metadata for an agent (durable-UID naming, STEA-004 §9).

    The persistent identity (agent_id UUID, external_registration_id) is
    IMMUTABLE (REQ-001) — only display_name / legacy_name / worker_uid change.
    """
    agent = await patch_agent_display(db, agent_id, payload)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent
