"""Memory/Session Offload models (flight plan Phase 8; ACMS-REQ-055..058).
Budget models: ACMS-REQ-059 (REV2 plan §4).

New concepts (checked against existing schema — extends, never duplicates):
- ExecutionSession: one disposable reasoning session under a persistent agent
  identity, bound to a Work/assignment, with economic telemetry.
- ContextPackage: the selected context handed to a session (references, not
  duplicates of large sources).
- SessionCheckpoint: rotation/rotation-advisory snapshots (handoff-linked).

Economic rotation is ADVISORY this slice (GREEN/MONITOR/CHECKPOINT_RECOMMENDED/
ROTATE_RECOMMENDED) — no auto-kill of active sessions (plan §13).
Budget tracking lives on the Work Item (REQ-059): soft_usd + hard_usd +
budget_state; per-Work rollups derive from session telemetry.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Budget states (REQ-059): OK → SOFT_EXCEEDED (advisory) → HARD_EXCEEDED
# (blocks new cloud execution absent an audited override).
BUDGET_OK = "OK"
BUDGET_SOFT_EXCEEDED = "SOFT_EXCEEDED"
BUDGET_HARD_EXCEEDED = "HARD_EXCEEDED"


class WorkBudgetRecord(Base):
    """Per-Work cloud budget (REQ-059). Separate table: Work Items keep their
    scope semantics; budget is execution-economics, versioned independently.

    Hard threshold blocks NEW cloud execution (never interrupts in-flight
    atomic work); override requires an explicit audited call.
    """

    __tablename__ = "work_budgets"

    work_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    soft_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    hard_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # OK | SOFT_EXCEEDED | HARD_EXCEEDED
    budget_state: Mapped[str] = mapped_column(String(20), default=BUDGET_OK)
    # audited override of a hard block (one-shot flag; cleared on next budget set)
    override_active: Mapped[bool] = mapped_column(default=False)
    override_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                 onupdate=_now)

    @staticmethod
    def now() -> datetime:
        return _now()


class ExecutionSessionRecord(Base):
    __tablename__ = "execution_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    work_item_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    assignment_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    a2a_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    harness_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # OPEN | CHECKPOINTED | CLOSED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # economic telemetry (REQ-058): current-context separate from cumulative
    current_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    cumulative_api_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # advisory state (REQ-059 soft side): GREEN|MONITOR|CHECKPOINT_RECOMMENDED|ROTATE_RECOMMENDED
    rotation_advisory: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Reserved cost split (REV2 §4.1): cloud vs local. Nullable — unknown cost
    # stays UNKNOWN, never zero/fabricated (local USD-equivalent not yet
    # authoritative; see FUTURE_WORK FW-LLM-LOCAL-COST).
    estimated_cloud_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_local_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # lightweight task classification (REV2 §9B.5) for economics by task type
    task_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    context_package_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    last_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handoff_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())

    @staticmethod
    def now() -> datetime:
        return _now()


class ContextPackageRecord(Base):
    """The selected context handed to a session (REQ-056/060).

    Sources are REFERENCES (type + id/uri + optional excerpt) — never full
    document copies when stable references suffice (plan §13).
    """

    __tablename__ = "context_packages"

    context_package_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # OPEN | SEALED (sealed when handed to a session)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    assembled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of source refs
    estimated_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())

    @staticmethod
    def now() -> datetime:
        return _now()


class SessionCheckpointRecord(Base):
    """Rotation checkpoints (REQ-057): what a session leaves behind mid-flight."""

    __tablename__ = "session_checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    work_item_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)  # budget|manual|rotate
    summary: Mapped[str] = mapped_column(String(512))
    # handoff-completeness validator result (REQ-057): JSON {field: bool}
    completeness_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    complete: Mapped[bool | None] = mapped_column(nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_refs_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())

    @staticmethod
    def now() -> datetime:
        return _now()


# Handoff-completeness required fields (REQ-057) — validator input contract
HANDOFF_REQUIRED_FIELDS = (
    "work_id", "status", "git_state", "tests", "deployment",
    "decisions", "debt", "remaining_work", "next_action", "artifacts",
)
