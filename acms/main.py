from fastapi import Depends, FastAPI, Response, status
from sqlalchemy.orm import Session

from . import __version__
from .db import Base, SessionLocal, engine
from .models import AgentRegistrationRequest, AgentResponse
from .registry import list_agents, register_agent
from .security import require_admin_token

Base.metadata.create_all(bind=engine)

app = FastAPI(title="AgentifyMe Cloud Management System", version=__version__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__}


@app.post("/api/v1/agents/register", response_model=AgentResponse)
def register(
    payload: AgentRegistrationRequest,
    response: Response,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> AgentResponse:
    agent, created = register_agent(db, payload)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return agent


@app.get("/api/v1/agents", response_model=list[AgentResponse])
def agents(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin_token),
) -> list[AgentResponse]:
    return list_agents(db)
