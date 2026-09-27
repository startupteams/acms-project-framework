"""Live fleet telemetry: heartbeat status, telemetry samples, semantic events,
session bindings (combined slice 3+4; ADR-0010; ACMS-REQ-033/034/035/052/053/054).

State dimensions stay separate (ADR-0010):
- connectivity: derived by ACMS from contact recency (UNKNOWN/HEALTHY/STALE/UNREACHABLE)
- agent_running: harness-reported tri-state (true/false/null)
- A2A task state: owned by the A2A protocol (not modeled here)
- management state: Work Items / Assignments (work_models) — never mutated by telemetry

Also: Pydantic models for the versioned heartbeat payload (acms-heartbeat-v1).
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

HEARTBEAT_SCHEMA_VERSION = "acms-heartbeat-v1"

# Connectivity states (ACMS-derived). UNKNOWN: never contacted; HEALTHY:
# recent contact; STALE: no contact > stale threshold; UNREACHABLE: stale
# >= reconcile threshold AND active reconciliation failed.
CONNECTIVITY_UNKNOWN = "UNKNOWN"
CONNECTIVITY_HEALTHY = "HEALTHY"
CONNECTIVITY_STALE = "STALE"
CONNECTIVITY_UNREACHABLE = "UNREACHABLE"

CONTACT_SOURCE_HEARTBEAT = "heartbeat"
CONTACT_SOURCE_RECONCILIATION = "reconciliation"
CONTACT_SOURCE_REGISTRATION = "registration"

# Context warning levels (plan §7).
CONTEXT_ELEVATED_DEFAULT = 70
CONTEXT_HIGH_DEFAULT = 85
CONTEXT_CRITICAL_DEFAULT = 95


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentStatusCurrentRecord(Base):
    """One latest-status row per agent (updated every heartbeat)."""

    __tablename__ = "agent_status_current"

    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.agent_id"), primary_key=True
    )
    # ACMS-derived
    connectivity: Mapped[str] = mapped_column(String(16), default=CONNECTIVITY_UNKNOWN)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contact_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Harness-reported tri-state snapshot: "true" | "false" | "unknown"
    agent_running: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Harness session correlation (REQ-053)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Expected assignment correlation (denormalized for display/alignment)
    expected_work_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_assignment_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    alignment: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ALIGNED/UNKNOWN/MISMATCH
    # Model/context telemetry (REQ-054) — null = not reported; invalid → *_invalid flags
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_used_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_telemetry_valid: Mapped[bool | None] = mapped_column(nullable=True)
    context_warning: Mapped[str | None] = mapped_column(String(16), nullable=True)  # NORMAL/ELEVATED/HIGH/CRITICAL
    context_max_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Usage (separate dimensions per plan §6)
    session_total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cumulative_api_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Platform/bridge info
    bridge_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bridge_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    harness_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platforms_connected: Mapped[str | None] = mapped_column(String(255), nullable=True)  # csv
    heartbeat_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @staticmethod
    def now() -> datetime:
        return _now()


class AgentTelemetrySampleRecord(Base):
    """Periodic + on-material-change telemetry samples (trends, e.g. context growth)."""

    __tablename__ = "agent_telemetry_samples"

    sample_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.agent_id"), index=True, nullable=False
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agent_running: Mapped[str | None] = mapped_column(String(16), nullable=True)  # true/false/unknown
    connectivity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context_used_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_warning: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cumulative_api_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @staticmethod
    def new_id() -> str:
        return str(__import__("uuid").uuid4())


class AgentEventRecord(Base):
    """Durable semantic event (ACMS-REQ-035 / plan §11)."""

    __tablename__ = "agent_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.agent_id"), nullable=True, index=True
    )
    work_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    assignment_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    a2a_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    harness_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    @staticmethod
    def new_id() -> str:
        return str(__import__("uuid").uuid4())


class AgentSessionBindingRecord(Base):
    """Latest observed harness session per agent (REQ-052/053 correlation)."""

    __tablename__ = "agent_session_bindings"

    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.agent_id"), primary_key=True
    )
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_running: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @staticmethod
    def now() -> datetime:
        return _now()


# ---------------------------------------------------------------- heartbeat payload models


class HeartbeatIdentity(BaseModel):
    acms_agent_id: str
    bridge_id: str | None = None
    bridge_version: str | None = None
    harness: str = "hermes"
    harness_version: str | None = None
    protocol_version: str | None = None


class HeartbeatAssignment(BaseModel):
    acms_work_key: str | None = None
    acms_assignment_key: str | None = None


class HeartbeatSession(BaseModel):
    session_id: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    last_activity_at: datetime | None = None
    agent_running: bool | None = None  # tri-state via None


class HeartbeatModel(BaseModel):
    model_id: str | None = None
    provider: str | None = None


class HeartbeatContext(BaseModel):
    used_tokens: int | None = None
    max_tokens: int | None = None
    utilization_percent: float | None = None
    max_source: str | None = None  # where max came from; never fabricated


class HeartbeatUsage(BaseModel):
    session_total_tokens: int | None = None
    cumulative_api_tokens: int | None = None


class HeartbeatPayload(BaseModel):
    """acms-heartbeat-v1 (plan §6). Unknown fields are rejected so schema
    drift is caught immediately rather than silently dropped."""

    schema_version: str = "acms-heartbeat-v1"
    identity: HeartbeatIdentity
    observed_at: datetime | None = None
    assignment: HeartbeatAssignment = HeartbeatAssignment()
    session: HeartbeatSession = HeartbeatSession()
    model: HeartbeatModel | None = None
    context: HeartbeatContext = HeartbeatContext()
    usage: HeartbeatUsage = HeartbeatUsage()
    platforms: dict[str, Any] = Field(default_factory=dict)
