"""Engineering-economics models (REV2 plan §9B; extends ACMS-REQ-058/061).

Canonical outcome metrics:
  Cost per Accepted PR      = all attributable cost / accepted PRs
  Cost per Accepted Req.    = all attributable cost / accepted requirements
Failed/retried runs stay IN the cost (never divided away). ACCEPTED is
recorded only from an explicit human/product acceptance event — "merged"
alone never implies accepted (unknown stays unknown).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Explicit outcome states (REV2 §9B.3). ACCEPTED only via explicit acceptance
# event; REVERTED/REWORK_REQUIRED are negative outcomes that keep their cost.
OUTCOME_OPEN = "OPEN"
OUTCOME_MERGED = "MERGED"
OUTCOME_CI_VERIFIED = "CI_VERIFIED"
OUTCOME_DEPLOYED = "DEPLOYED"
OUTCOME_LIVE_VERIFIED = "LIVE_VERIFIED"
OUTCOME_ACCEPTED = "ACCEPTED"
OUTCOME_REWORK_REQUIRED = "REWORK_REQUIRED"
OUTCOME_REVERTED = "REVERTED"
OUTCOME_STATES = (
    OUTCOME_OPEN, OUTCOME_MERGED, OUTCOME_CI_VERIFIED, OUTCOME_DEPLOYED,
    OUTCOME_LIVE_VERIFIED, OUTCOME_ACCEPTED, OUTCOME_REWORK_REQUIRED,
    OUTCOME_REVERTED,
)

# Lightweight task classification (REV2 §9B.5)
TASK_CATEGORIES = (
    "architecture", "backend", "frontend", "database", "infrastructure",
    "debugging", "testing", "documentation", "review", "research",
)


class PrOutcomeRecord(Base):
    """Outcome record for one PR/branch execution effort."""

    __tablename__ = "pr_outcomes"

    outcome_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    commit_shas_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # see OUTCOME_STATES; ACCEPTED requires explicit acceptance event
    outcome_state: Mapped[str] = mapped_column(String(20), default=OUTCOME_OPEN, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    session_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # optional human-time inputs — never invented, nullable until recorded
    human_review_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    human_repair_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_clock_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_pass: Mapped[bool | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                 onupdate=_now)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())


class RequirementLinkRecord(Base):
    """Many-to-many PR ↔ requirement correlation (REV2 §9B.2)."""

    __tablename__ = "requirement_links"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    outcome_id: Mapped[str] = mapped_column(String(36), index=True)
    requirement_ref: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())


class CostAttributionRecord(Base):
    """Per-execution cost correlated to a PR outcome (REV2 §9B.4).

    ``failed_run`` rows keep their cost in every rollup — failed attempts are
    part of cost-per-accepted, never divided away. Local USD-equivalent stays
    nullable until an authoritative source exists (FW-LLM-LOCAL-COST).
    """

    __tablename__ = "cost_attribution"

    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    outcome_id: Mapped[str] = mapped_column(String(36), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_compute_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # session | aggregate | manual
    source: Mapped[str] = mapped_column(String(32), default="session")
    failed_run: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())
