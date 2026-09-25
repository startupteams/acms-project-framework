from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import uuid

from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class TrustClass(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class AgentRecord(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_registration_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    trust_class: Mapped[str] = mapped_column(String(32))
    harness: Mapped[str] = mapped_column(String(128))
    bridge_version: Mapped[str] = mapped_column(String(64))
    protocol_version: Mapped[str] = mapped_column(String(64))
    card_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    capability_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capability_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)


class AgentCapabilities(BaseModel):
    streaming: bool = False
    pause: bool = False
    resume: bool = False
    interrupt: bool = False
    cancel: bool = False
    steering: bool = False
    transcript_export: bool = False
    background_routine_inventory: bool = False
    extensions: list[str] = Field(default_factory=list)


class AgentRegistrationRequest(BaseModel):
    external_registration_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    trust_class: TrustClass
    harness: str = Field(min_length=1, max_length=128)
    bridge_version: str = Field(min_length=1, max_length=64)
    protocol_version: str = Field(min_length=1, max_length=64)
    card_url: HttpUrl | None = None
    capability_hash: str | None = Field(default=None, max_length=128)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)


class AgentResponse(BaseModel):
    agent_id: str
    external_registration_id: str
    display_name: str
    trust_class: TrustClass
    harness: str
    bridge_version: str
    protocol_version: str
    card_url: str | None
    capability_hash: str | None
    capabilities: AgentCapabilities
    created_at: datetime
    updated_at: datetime
