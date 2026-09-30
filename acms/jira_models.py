"""Jira kickoff linkage + reconciliation tables (window-5 plan §7/§13).

Four concerns, all durable (no in-memory state survives restarts):

1. Jira linkage ON WorkItemRecord (columns added in work_models; migration 0010)
   — site/issue-id/key/url + last observation + eligibility verdict.

2. ``jira_issue_observations`` — append-only history of Jira reads (status,
   assignee accountId) tied to a work item + issue id. Never mutated; the
   newest row is the last observation.

3. ``work_runtime_holds`` — persistent local PAUSE/STOP controls (§13.3).
   A hold survives process restarts and polls; only an explicit operator clear
   (``cleared_at``) removes it. Pausing execution must not disable Jira
   reconciliation — holds are honored by the gate, not by hiding the issue.

4. ``jira_reconciliation_runs`` — durable ledger for every reconciliation run
   (manual "Check Jira now", scheduled poll, bootstrap start request — all go
   through ONE service). Idempotency/deduplication keys live here too
   (``lease_owner``/``lease_expires_at`` for §13.4 step 1; counts per outcome).

5. ``jira_issue_links`` — unique (site_id, issue_id) ↔ top-level work item
   authorization. One Jira issue MAY authorize multiple Work Items (§7.3);
   the LINK row records the intake issue; descendants inherit via parent walk.
   Uniqueness enforces "one intake issue" dedupe for top-level creation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class JiraIssueObservationRecord(Base):
    """Append-only Jira observation history (plan §13.4 step 3)."""

    __tablename__ = "jira_issue_observations"

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_items.work_item_id", ondelete="SET NULL"), nullable=True, index=True
    )
    jira_site_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    jira_issue_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    observed_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_assignee_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # what produced the read: dispatch_gate | scheduled_poll | manual_check | bootstrap_request
    source: Mapped[str] = mapped_column(String(32), default="manual_check")
    # read completeness: ok | partial | failed (§13.4 step 3 — partial never advances cursors)
    read_outcome: Mapped[str] = mapped_column(String(16), default="ok")
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def new_id() -> str:
        return new_id()


class WorkRuntimeHoldRecord(Base):
    """Persistent local PAUSE/STOP hold (§13.3) — survives restarts and polls."""

    __tablename__ = "work_runtime_holds"

    hold_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_items.work_item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # PAUSE | STOP
    hold_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # scheduler | manual_check | ui | api
    source: Mapped[str] = mapped_column(String(32), default="ui")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cleared_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # acknowledgment trail (§13.5: requested vs acknowledged are DISTINCT facts)
    runtime_acknowledged: Mapped[bool | None] = mapped_column(Boolean(), default=None, nullable=True)
    ack_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def new_id() -> str:
        return new_id()


class JiraReconciliationRunRecord(Base):
    """Durable reconciliation-run ledger (§13.4/§13.5/§13.6)."""

    __tablename__ = "jira_reconciliation_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # manual_check | scheduled_poll | bootstrap_request | startup_catchup
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # requested_by: username (manual) or scheduler identity (system)
    requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # queued | running | completed | partial | failed
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # lease fields (§13.4 step 1): overlapping requests coalesce to the live run
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # per-outcome counts (§13.5 required display)
    examined_count: Mapped[int] = mapped_column(Integer, default=0)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    started_count: Mapped[int] = mapped_column(Integer, default=0)
    resumed_count: Mapped[int] = mapped_column(Integer, default=0)
    paused_count: Mapped[int] = mapped_column(Integer, default=0)
    paused_acknowledged_count: Mapped[int] = mapped_column(Integer, default=0)
    stopped_count: Mapped[int] = mapped_column(Integer, default=0)
    stopped_acknowledged_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    # paginated-scan completeness (§13.4 step 3): full | partial | failed
    scan_completeness: Mapped[str] = mapped_column(String(16), default="full")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    @staticmethod
    def new_id() -> str:
        return new_id()


class JiraSyncSchedulerStateRecord(Base):
    """Single-row durable scheduler bookkeeping (§13.6) — UTC timestamps."""

    __tablename__ = "jira_scheduler_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_successful_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class JiraIssueLinkRecord(Base):
    """(site_id, issue_id) ↔ top-level Work Item authorization (§7.3)."""

    __tablename__ = "jira_issue_links"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    jira_site_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    jira_issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    jira_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # the TOP-LEVEL work item carrying the authorization
    work_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("work_items.work_item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # generation accounting (§13.2): how many execution generations this
    # authorization has produced; a stopped/completed generation never
    # restarts on a re-polled identical ready snapshot.
    generation_count: Mapped[int] = mapped_column(Integer, default=0)
    last_ready_observation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @staticmethod
    def new_id() -> str:
        return new_id()
