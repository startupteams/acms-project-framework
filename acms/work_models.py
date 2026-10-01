from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class WorkItemStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkItemKind(StrEnum):
    PRODUCT = "product"
    PROJECT = "project"
    FEATURE = "feature"
    TASK = "task"


class WorkItemRecord(Base):
    """Hierarchical work model (ACMS-REQ-007).

    A work item traces to its parent (project/feature) via ``parent_id``.
    Execution tasks (A2A runs) reference a work item (ACMS-REQ-036).
    """

    __tablename__ = "work_items"

    work_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Immutable human-readable key (ACMS-REQ-052): ACMS-WORK-000001 style.
    # Nullable for pre-slice rows; allocated durably via acms/work_keys.py.
    work_key: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    # Immutable human UID (STEA-004 plan §6): ACMS-WORK-######-YYYYMMDD_HHMMSS.
    # Same sequence as work_key; UUID remains THE identity (REQ-001).
    work_uid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    # Monotonic sequence number backing work_uid (§6 backfill determinism).
    work_sequence: Mapped[int | None] = mapped_column(nullable=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_items.work_item_id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default=WorkItemKind.TASK.value)
    title: Mapped[str] = mapped_column(String(255))
    # Provenance of approved scope (ACMS-REQ-010/011): who created this item.
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Project home (STEA-004 plan §9; ADR-0017). Nullable for legacy rows;
    # new items should carry one (API layer enforces for new creation).
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    status: Mapped[str] = mapped_column(String(16), default=WorkItemStatus.PLANNED.value)
    # Human-readable Markdown scope/requirements (ACMS-REQ-011: approved scope is authoritative).
    scope_markdown: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # ---- Jira kickoff linkage (window-5 §7) --------------------------------
    # Persisted observation state is SEPARATE from desired ACMS state (§13.2).
    jira_site_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    jira_issue_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    jira_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    jira_last_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jira_last_assignee_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    jira_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ELIGIBLE | NOT_LINKED | NOT_READY | ASSIGNEE_MISMATCH | LOCAL_HOLD | JIRA_UNKNOWN
    jira_eligibility: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    jira_eligibility_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)


class AssignmentRecord(Base):
    """Primary work assignment (ACMS-REQ-008: at most one ACTIVE primary per agent).

    Assignment lifecycle: ACTIVE -> COMPLETED | RELEASED | SUSPENDED.
    A new ACTIVE assignment for an agent requires the previous one to be in a
    terminal/released state; the API enforces this invariant.
    """

    __tablename__ = "work_assignments"

    assignment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Immutable human-readable key (ACMS-REQ-052): ACMS-ASG-000001 style.
    assignment_key: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    # Dispatch/correlation facts (combined slice 3+4; facts, not A2A state).
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bound_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.agent_id"), index=True)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_items.work_item_id"), index=True)
    # ACTIVE | COMPLETED | RELEASED | SUSPENDED
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    assigned_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionTaskRecord(Base):
    """A2A execution task linked to a work item (ACMS-REQ-036).

    One work item may have many execution tasks (retry/resume/review) without
    creating a new management work item.
    """

    __tablename__ = "execution_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_items.work_item_id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agents.agent_id"), nullable=True, index=True)
    # A2A task id from the bridge (may be empty until first execution starts)
    external_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # RUNNING | SUCCEEDED | FAILED | CANCELLED
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ---- A2A delivery contract (ADR-0013; STEA-004 plan §12) ----------------
    # DISPATCHING = envelope sent, awaiting ACK; RUNNING = run id received.
    # NULL for legacy rows (pre-contract dispatches) — treated as RUNNING.
    transport_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    assignment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Model policy snapshot (ADR-0015): resolved policy + effective model.
    model_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_resolution_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ttft_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failover_attempt: Mapped[int] = mapped_column(default=0)


class BackgroundRoutineRecord(Base):
    """Background routine inventory (ACMS-REQ-014).

    Routines are declared/reported by agents; ACMS records inventory and the
    latest reported status but does not schedule or authorize each execution.
    """

    __tablename__ = "background_routines"

    routine_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.agent_id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    # latest reported status/outcome (free text, agent-reported)
    latest_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latest_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HandoffRecord(Base):
    """Structured Markdown handoff (ACMS-REQ-013), linkable from work-item history."""

    __tablename__ = "work_handoffs"

    handoff_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("work_items.work_item_id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agents.agent_id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    body_markdown: Mapped[str] = mapped_column(Text)
    # observed-vs-interpretation distinction is a content convention, not schema
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- Pydantic API models


class WorkItemCreate(BaseModel):
    kind: WorkItemKind = WorkItemKind.TASK
    parent_id: str | None = Field(default=None, max_length=36)
    title: str = Field(min_length=1, max_length=255)
    created_by: str | None = Field(default=None, max_length=128)
    scope_markdown: str = ""
    project_id: str | None = Field(default=None, max_length=36)


class WorkItemUpdate(BaseModel):
    status: WorkItemStatus | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    scope_markdown: str | None = None
    project_id: str | None = Field(default=None, max_length=36)


class WorkItemResponse(BaseModel):
    work_item_id: str
    parent_id: str | None
    kind: str
    title: str
    created_by: str | None
    project_id: str | None = None
    status: str
    scope_markdown: str
    created_at: datetime
    updated_at: datetime
    work_key: str | None = None
    work_uid: str | None = None


class AssignmentCreate(BaseModel):
    agent_id: str = Field(max_length=36)
    work_item_id: str = Field(max_length=36)


class AssignmentResponse(BaseModel):
    assignment_id: str
    agent_id: str
    work_item_id: str
    status: str
    assigned_by: str | None
    assigned_at: datetime
    closed_at: datetime | None


class ExecutionTaskCreate(BaseModel):
    work_item_id: str = Field(max_length=36)
    agent_id: str | None = Field(default=None, max_length=36)
    external_task_id: str | None = Field(default=None, max_length=128)
    # A2A delivery contract (ADR-0013) + model policy snapshot (ADR-0015)
    transport_state: str | None = Field(default=None, max_length=16)
    assignment_id: str | None = Field(default=None, max_length=36)
    idempotency_key: str | None = Field(default=None, max_length=64)
    model_policy_json: str | None = None
    effective_model: str | None = Field(default=None, max_length=128)
    model_resolution_reason: str | None = Field(default=None, max_length=32)
    ttft_deadline_at: datetime | None = None


class ExecutionTaskResponse(BaseModel):
    task_id: str
    work_item_id: str
    agent_id: str | None
    external_task_id: str | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    transport_state: str | None = None
    effective_model: str | None = None
    model_resolution_reason: str | None = None


class RoutineUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    purpose: str | None = None
    schedule: str | None = Field(default=None, max_length=128)
    enabled: bool = True
    latest_status: str | None = Field(default=None, max_length=255)


class RoutineResponse(BaseModel):
    routine_id: str
    agent_id: str
    name: str
    purpose: str | None
    schedule: str | None
    enabled: bool
    latest_status: str | None
    latest_report_at: datetime | None
    created_at: datetime


class HandoffCreate(BaseModel):
    work_item_id: str = Field(max_length=36)
    agent_id: str | None = Field(default=None, max_length=36)
    title: str = Field(min_length=1, max_length=255)
    body_markdown: str = Field(min_length=1)


class HandoffResponse(BaseModel):
    handoff_id: str
    work_item_id: str
    agent_id: str | None
    title: str
    body_markdown: str
    created_at: datetime
