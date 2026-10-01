"""A2A production path models (STEA-004 plan 2026-10-01; ADRs 0013-0018).

Projects/Products/Tags (plan §9), model policy hierarchy (plan §17),
execution events (plan §15 live stream), human inbox (plan §23),
artifacts (plan §25). All additive; existing tables untouched except
work_items.project_id (nullable FK) and execution_tasks transport columns.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- projects/products


class ProductRecord(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    business_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    default_model_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)


class ProjectRecord(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.product_id", ondelete="SET NULL"), nullable=True, index=True
    )
    repos: Mapped[str | None] = mapped_column(Text, nullable=True)
    jira_project_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    default_model_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)


# ---------------------------------------------------------------- tags


class TagRecord(Base):
    __tablename__ = "tags"

    tag_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)


class TagLinkRecord(Base):
    __tablename__ = "tag_links"
    __table_args__ = (UniqueConstraint("tag_id", "scope", "scope_id", name="uq_tag_link"),)

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tag_id: Mapped[str] = mapped_column(String(36), ForeignKey("tags.tag_id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(16))  # work_item | project | product
    scope_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)


# ---------------------------------------------------------------- model policy


class ModelPolicyRecord(Base):
    """Resolved-by-precedence model policy (ADR-0015). One row per scope."""

    __tablename__ = "model_policies"
    __table_args__ = (UniqueConstraint("scope", "scope_id", name="uq_model_policy_scope"),)

    policy_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))  # system|agent|project|work_item
    scope_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    inference_policy: Mapped[str] = mapped_column(String(24), default="local-preferred")
    preferred_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cloud_fallback: Mapped[bool] = mapped_column(default=False)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)


# ---------------------------------------------------------------- execution events


class ExecutionEventRecord(Base):
    """Live activity events for one execution task (ADR-0014)."""

    __tablename__ = "execution_events"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_execution_event_seq"),)

    event_pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("execution_tasks.task_id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- human inbox


class HumanInboxItemRecord(Base):
    __tablename__ = "human_inbox_items"

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_class: Mapped[str] = mapped_column(String(24), default="ACTION_REQUIRED")
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(String(120), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True, index=True
    )
    work_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_items.work_item_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    execution_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.product_id", ondelete="SET NULL"), nullable=True
    )
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_force: Mapped[bool] = mapped_column(default=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)


# ---------------------------------------------------------------- artifacts


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_items.work_item_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    execution_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.product_id", ondelete="SET NULL"), nullable=True
    )
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(32), default="handoff")
    title: Mapped[str] = mapped_column(String(255))
    bluf: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(64), default="text/markdown")
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    new_id = staticmethod(_new_id)
    now = staticmethod(_now)

    @staticmethod
    def sha(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()