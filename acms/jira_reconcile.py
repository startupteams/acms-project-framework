"""ONE durable Jira reconciliation service (window-5 plan §13.4–§13.6).

Every trigger — manual "Check Jira now", the durable scheduled poll, bootstrap
start requests, (future) webhooks — calls :func:`reconcile` through
:func:`start_reconciliation_run`, which:

1. coalesces overlapping requests via a durable lease (one live run; duplicate
   clicks return the EXISTING run id — idempotent),
2. fetches ALL paginated ready/AI-assigned candidate issues AND re-fetches
   every linked nonterminal issue by stable id regardless of current
   assignment/status (a ready-only search cannot detect withdrawal),
3. records append-only observations (partial scans marked partial — never
   advanced as success),
4. computes desired state per §13.2 (mapping table, precedence, generations),
5. applies pause/stop BEFORE new starts for the same goal,
6. emits durable audit events; idsempotent dispatch via generation keys.

State-mapping core (§13.2) — observed Jira state drives ACMS:

| Observation                                   | ACMS action                       |
|-----------------------------------------------|-----------------------------------|
| Ready + AI assignee, no prior start            | import/queue/start (gen 1)       |
| Ready + AI assignee, already queued/running    | reconcile, no duplicate          |
| In Progress + AI assignee + proven gen         | continue/reconcile               |
| In Progress + AI assignee, no proven ready     | Attention; never infer auth      |
| On Hold/Paused/Blocked                         | reversible pause fan-out         |
| Backlog/non-ready after kickoff                | pause; unstarted never start     |
| Reassigned away / unassigned                   | pause immediately; block dispatch|
| In Review                                      | stop launching; checkpoint       |
| Canceled/Won't Do                              | stop generation + descendants    |
| Done/Closed                                    | stop remaining; no fake evidence |
| Unknown/unavailable/read failure               | unknown; inhibit; Attention      |

Precedence: explicit terminal/cancel > local stop/pause > assignment mismatch
> unknown > non-ready. Eligibility NEVER overrides budget/hold checks.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .jira_client import JiraClient, JiraError
from .settings import get_settings
from .jira_gate import (ASSIGNEE_MISMATCH, ELIGIBLE, HOLD_PAUSE, HOLD_STOP,
                        LOCAL_HOLD, NOT_LINKED, NOT_READY, STALE_AUTHORIZATION,
                        UNKNOWN, ai_account_id_setting, evaluate_observation,
                        ready_statuses)
from .jira_models import (JiraIssueLinkRecord, JiraIssueObservationRecord,
                          JiraReconciliationRunRecord, WorkRuntimeHoldRecord,
                          new_id)
from .telemetry_service import add_event
from .work_models import WorkItemRecord

# Jira status names we treat as terminal-cancel (verified per project workflow
# at mapping time; unknown names NEVER map to cancel — §13.1).
CANCEL_STATUS_NAMES = {"CANCELED", "CANCELLED", "WON'T DO", "WONT DO"}
DONE_STATUS_NAMES = {"DONE", "CLOSED", "COMPLETED"}
PAUSE_STATUS_NAMES = {"ON HOLD", "PAUSED", "BLOCKED", "DEFERRED"}
IN_PROGRESS_STATUS_NAMES = {"IN PROGRESS"}
IN_REVIEW_STATUS_NAMES = {"IN REVIEW"}

# Observations older than this are treated as stale for decisions that claim
# continuity (§13.2: stale decisions rejected before start/resume).
_OBSERVATION_MAX_AGE_S = 15 * 60

# Scheduler identity (single-replica discipline TDR-0009; multi-replica needs
# the lease below + leader election — documented).
SCHEDULER_ACTOR = "jira-reconcile-scheduler"


@dataclass
class ReconcileCounts:
    examined: int = 0
    added: int = 0
    started: int = 0
    resumed: int = 0
    paused: int = 0
    paused_acknowledged: int = 0
    stopped: int = 0
    stopped_acknowledged: int = 0
    unchanged: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "examined": self.examined, "added": self.added, "started": self.started,
            "resumed": self.resumed, "paused": self.paused,
            "paused_acknowledged": self.paused_acknowledged,
            "stopped": self.stopped, "stopped_acknowledged": self.stopped_acknowledged,
            "unchanged": self.unchanged, "failed": self.failed,
        }


@dataclass
class ReconcileOutcome:
    run_id: str
    state: str            # queued | running | completed | partial | failed
    counts: ReconcileCounts = field(default_factory=ReconcileCounts)
    coalesced: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "state": self.state,
                "counts": self.counts.to_dict(), "coalesced": self.coalesced,
                "error": self.error}


# ------------------------------------------------------------------ lease/runs

_LEASE_TTL_S = 600  # bounded lease; crashed runs expire (§13.4 step 1)


async def start_reconciliation_run(db: AsyncSession, *, trigger: str,
                                   requested_by: str | None,
                                   execute: bool = True) -> ReconcileOutcome:
    """Create (or coalesce into) a reconciliation run.

    Duplicate clicks / overlapping polls: an existing queued/running run with a
    valid lease returns ITS id (coalesced=True). A crashed run (lease expired)
    is superseded by a new run.
    """
    now = datetime.now(timezone.utc)
    live = (await db.scalars(
        select(JiraReconciliationRunRecord)
        .where(JiraReconciliationRunRecord.state.in_(("queued", "running")))
        .order_by(JiraReconciliationRunRecord.created_at.desc())
        .limit(1)
    )).first()
    if live is not None:
        lease_exp = live.lease_expires_at
        # SQLite-loaded datetimes may be naive — normalize before comparing
        if lease_exp is not None and lease_exp.tzinfo is None:
            lease_exp = lease_exp.replace(tzinfo=timezone.utc)
        if lease_exp is not None and lease_exp > now:
            return ReconcileOutcome(run_id=live.run_id, state=live.state,
                                    coalesced=True)

    run = JiraReconciliationRunRecord(
        run_id=new_id(), trigger=trigger, requested_by=requested_by,
        state="queued", lease_owner=requested_by or SCHEDULER_ACTOR,
        lease_expires_at=now + timedelta(seconds=_LEASE_TTL_S),
    )
    db.add(run)
    await add_event(
        db, event_type="JIRA_RECONCILE_REQUESTED", actor_source="jira_reconcile",
        summary=f"reconciliation requested ({trigger}) by {requested_by or SCHEDULER_ACTOR}",
        metadata={"run_id": run.run_id, "trigger": trigger,
                  "coalesced_from": live.run_id if live is not None and live.state == "running" else None},
    )
    await db.commit()

    if not execute:
        return ReconcileOutcome(run_id=run.run_id, state="queued")

    return await execute_reconciliation(db, run)


# ------------------------------------------------------------------ the service

async def execute_reconciliation(db: AsyncSession, run: JiraReconciliationRunRecord) -> ReconcileOutcome:
    """Run ONE reconciliation pass. All triggers converge here."""
    from .jira_models import JiraSyncSchedulerStateRecord  # scheduler bookkeeping

    now = datetime.now(timezone.utc)
    run.state = "running"
    run.started_at = now
    await db.commit()

    counts = ReconcileCounts()
    client = JiraClient()
    problems: list[str] = []
    scan_completeness = "full"

    if client.is_mock:
        # No credentials configured: report honestly; never fabricate success.
        run.state = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.error_summary = "Jira integration not configured (mock mode); reconciliation cannot run"
        await add_event(db, event_type="JIRA_RECONCILE_FAILED", actor_source="jira_reconcile",
                        summary=run.error_summary, metadata={"run_id": run.run_id})
        await db.commit()
        return ReconcileOutcome(run_id=run.run_id, state="failed",
                                error=run.error_summary)

    # ---- step 2: fetch candidates (paginated) + all linked issues ----------
    candidates: list[dict[str, Any]] = []
    try:
        start_at = 0
        while True:
            page, nxt = client.search_ai_account_issues(max_results=50, start_at=start_at)
            candidates.extend({"issue": i, "via": "candidate_search"} for i in page)
            counts.examined += len(page)
            if nxt is None:
                break
            start_at = nxt
            if start_at > 1000:  # bounded safety
                scan_completeness = "partial"
                problems.append("candidate scan hit page bound; marked partial")
                break
    except JiraError as e:
        problems.append(f"candidate search failed: {str(e)[:160]}")
        scan_completeness = "failed"

    # Direct re-fetch of every LINKED issue (withdrawal detection — §13.4)
    linked_rows = (await db.scalars(select(JiraIssueLinkRecord))).all()
    for link in linked_rows:
        try:
            issue = client.get_issue_by_id(link.jira_issue_id)
            candidates.append({"issue": issue, "via": "linked_direct",
                               "link": link})
            counts.examined += 1
        except JiraError as e:
            problems.append(f"linked issue {link.jira_issue_key or link.jira_issue_id} read failed: "
                            f"{str(e)[:120]}")
            scan_completeness = "partial" if scan_completeness == "full" else "failed"
            counts.failed += 1
            await _observe_failure(db, link, str(e)[:200])

    # ---- step 3-5: observe + decide + act per issue -------------------------
    seen_links: set[str] = set()
    for item in candidates:
        issue: Any = item["issue"]
        link: JiraIssueLinkRecord | None = item.get("link")
        try:
            await _process_observation(db, run, issue, link, counts, seen_links)
        except Exception as e:  # per-issue isolation — one bad issue never aborts the pass
            problems.append(f"{issue.key}: {str(e)[:140]}")
            counts.failed += 1
            await _record_issue_outcome(
                db, run, issue, outcome="failed", reason=str(e)[:400])
    await db.commit()

    # ---- step 6: scheduler bookkeeping (durable, UTC) ------------------------
    sched = await _scheduler_state(db)
    if scan_completeness == "full" and not problems:
        sched.last_successful_poll_at = now
    sched.last_attempt_at = now
    await db.commit()

    run.state = "completed" if (scan_completeness == "full" and not problems) else (
        "partial" if scan_completeness != "failed" else "failed")
    run.finished_at = datetime.now(timezone.utc)
    run.scan_completeness = scan_completeness
    run.examined_count = counts.examined
    run.added_count = counts.added
    run.started_count = counts.started
    run.resumed_count = counts.resumed
    run.paused_count = counts.paused
    run.paused_acknowledged_count = counts.paused_acknowledged
    run.stopped_count = counts.stopped
    run.stopped_acknowledged_count = counts.stopped_acknowledged
    run.unchanged_count = counts.unchanged
    run.failed_count = counts.failed
    run.error_summary = "; ".join(problems)[:1000] or None
    await add_event(
        db, event_type="JIRA_RECONCILE_COMPLETED", actor_source="jira_reconcile",
        summary=(f"run {run.run_id[:8]} {run.state}: examined={counts.examined} "
                 f"started={counts.started} paused={counts.paused} stopped={counts.stopped} "
                 f"unchanged={counts.unchanged} failed={counts.failed}"),
        metadata={"run_id": run.run_id, **counts.to_dict(),
                  "scan_completeness": scan_completeness},
    )
    await db.commit()
    return ReconcileOutcome(run_id=run.run_id, state=run.state, counts=counts,
                            error=run.error_summary)


# ------------------------------------------------------------------ per-issue

async def _process_observation(db: AsyncSession, run: JiraReconciliationRunRecord,
                               issue: Any, link: JiraIssueLinkRecord | None,
                               counts: ReconcileCounts, seen_links: set[str]) -> None:
    now = datetime.now(timezone.utc)
    key = issue.key
    issue_id = str(getattr(issue, "issue_id", "") or "")
    status_u = (issue.status or "").strip().upper()
    assignee_account = issue.assignee_account_id

    # dedupe within one pass: candidate search + direct fetch may overlap
    dedupe_id = issue_id or key
    if dedupe_id in seen_links:
        return
    seen_links.add(dedupe_id)

    # find authorization link (for candidate-search-sourced issues)
    if link is None and issue_id:
        link = (await db.scalars(
            select(JiraIssueLinkRecord).where(JiraIssueLinkRecord.jira_issue_id == issue_id)
        )).first()

    # append-only observation
    await _observe(db, issue, link, source=("manual_check" if run.trigger == "manual_check"
                                            else "scheduled_poll"))

    if link is None:
        # Human intent is already explicit in Jira: the dedicated AI account is
        # the assignee AND the status is in the configured ready set.  Check Jira
        # now is explicitly execution-capable, so this service must create and
        # start the one high-level ACMS work item rather than silently observe it.
        verdict = evaluate_observation(
            status_name=issue.status,
            assignee_account_id=assignee_account,
            has_local_hold=False,
        )
        if not verdict.eligible:
            counts.unchanged += 1
            await _record_issue_outcome(
                db, run, issue, outcome="not_eligible", reason=verdict.reason)
            return
        result = await _intake_and_start(db, run, issue, counts)
        await _record_issue_outcome(
            db, run, issue, outcome=result["outcome"], reason=result["reason"],
            work_item_id=result.get("work_item_id"), agent_id=result.get("agent_id"),
            task_id=result.get("task_id"))
        return

    # ---- LINKED issue: fetch the authorized work item(s) --------------------
    work = await db.get(WorkItemRecord, link.work_item_id)
    if work is None:
        counts.failed += 1
        return
    hold_rows = (await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.work_item_id == work.work_item_id)
        .where(WorkRuntimeHoldRecord.cleared_at.is_(None))
    )).all()
    hold_kinds = [h.hold_kind for h in hold_rows]

    ai_id = ai_account_id_setting()
    assigned_to_ai = bool(ai_id) and assignee_account == ai_id

    # ---- §13.2 mapping (precedence: cancel > stop > pause > mismatch > ...) --
    if status_u in CANCEL_STATUS_NAMES:
        await _apply_stop(db, work, link, run, counts,
                          reason=f"Jira {key} → {issue.status}")
    elif status_u in DONE_STATUS_NAMES:
        # stop remaining; reconcile human final disposition; never fabricate
        await _apply_stop(db, work, link, run, counts,
                          reason=f"Jira {key} → {issue.status} (human final disposition)",
                          soft=True)
    elif not assigned_to_ai:
        await _apply_pause(db, work, link, run, counts,
                           reason=f"Jira {key} assignment withdrawn from AI account",
                           hold_kind=HOLD_PAUSE)
    elif status_u in PAUSE_STATUS_NAMES:
        await _apply_pause(db, work, link, run, counts,
                           reason=f"Jira {key} → {issue.status}")
    elif status_u in IN_REVIEW_STATUS_NAMES:
        # stop launching implementation; checkpoint into review-pending
        counts.unchanged += 1
        await add_event(db, event_type="JIRA_WORK_IN_REVIEW", actor_source="jira_reconcile",
                        work_key=work.work_key,
                        summary=f"Jira {key} in review — no new implementation dispatches; "
                                f"await human review or new ready transition",
                        metadata={"issue_key": key, "run_id": run.run_id})
    elif status_u in IN_PROGRESS_STATUS_NAMES:
        if not _has_proven_ready_generation(link):
            # In Progress with NO proven ready kickoff → Attention; never infer
            counts.unchanged += 1
            await add_event(db, event_type="JIRA_UNPROVEN_IN_PROGRESS", actor_source="jira_reconcile",
                            work_key=work.work_key,
                            summary=f"Jira {key} In Progress without a proven ready kickoff "
                                    f"generation — attention; authorization NOT inferred",
                            metadata={"issue_key": key, "run_id": run.run_id,
                                      "generation_count": link.generation_count})
        else:
            counts.unchanged += 1  # continue/reconcile current execution
    elif status_u in ready_statuses():
        await _apply_ready(db, work, link, run, counts, issue, hold_kinds)
    else:
        # Unknown/unmapped status → inhibit + Attention (§13.1)
        counts.unchanged += 1
        await add_event(db, event_type="JIRA_STATUS_UNKNOWN", actor_source="jira_reconcile",
                        work_key=work.work_key,
                        summary=f"Jira {key} status '{issue.status}' is not mapped — "
                                f"dispatch inhibited until the mapping is configured",
                        metadata={"issue_key": key, "status": issue.status, "run_id": run.run_id})

    await _record_issue_outcome(
        db, run, issue, outcome="reconciled",
        reason=f"linked work evaluated at Jira status {issue.status}",
        work_item_id=work.work_item_id,
    )


def _has_proven_ready_generation(link: JiraIssueLinkRecord) -> bool:
    """A generation exists only when a ready observation PRECEDED a start."""
    return (link.generation_count or 0) > 0 and link.last_ready_observation_at is not None \
        and link.last_generation_started_at is not None


async def _record_issue_outcome(
    db: AsyncSession, run: JiraReconciliationRunRecord, issue: Any, *,
    outcome: str, reason: str, work_item_id: str | None = None,
    agent_id: str | None = None, task_id: str | None = None,
) -> None:
    """Persist a queryable outcome for every examined issue in the audit log."""
    await add_event(
        db, event_type="JIRA_RECONCILIATION_OUTCOME", actor_source="jira_reconcile",
        agent_id=agent_id,
        summary=f"Jira {issue.key}: {outcome} — {reason}"[:512],
        metadata={
            "run_id": run.run_id, "issue_key": issue.key,
            "issue_id": str(getattr(issue, "issue_id", "") or ""),
            "outcome": outcome, "reason": reason[:512],
            "work_item_id": work_item_id, "agent_id": agent_id,
            "task_id": task_id,
        },
    )


def _intake_instruction(issue: Any, work_uid: str | None) -> str:
    description = (getattr(issue, "description", "") or "").strip()
    return (
        f"Execute Jira {issue.key} end-to-end for ACMS work {work_uid or 'pending UID'}.\n\n"
        f"Summary: {issue.summary}\n"
        f"Priority: {getattr(issue, 'priority', '') or 'unspecified'}\n"
        f"Jira URL: {issue.url}\n\n"
        f"Requested work:\n{description}\n\n"
        "Execution requirements:\n"
        "1. Work only in startupteams/acms-project-framework and follow repository AGENTS.md.\n"
        "2. Use branch AGENT_STEA004_ENTREPRENEUR; never push directly to main.\n"
        "3. Implement the smallest safe change, run the full test suite, open/merge a PR, and deploy "
        "with deploy/release.sh so rollback remains available.\n"
        "4. Verify production /version, /health, Alembic state, and the requested acceptance criteria.\n"
        "5. Create a concise Markdown handoff in the repository or work record with BLUF, changes, "
        "tests, production evidence, PR/SHA, rollback, and remaining work. Never include secrets.\n"
        "6. Do not transition Jira directly; ACMS owns policy-governed Jira updates after your run."
    )


async def _project_for_issue(db: AsyncSession, issue: Any):
    from .a2a_models import ProjectRecord

    key = (getattr(issue, "project_key", None) or issue.key.split("-")[0]).upper()
    return (await db.scalars(
        select(ProjectRecord).where(ProjectRecord.jira_project_key == key).limit(1)
    )).first()


async def _select_idle_worker(db: AsyncSession):
    """Pick a configured healthy/known Hermes worker with no active lock."""
    from .bridge import get_bridge_for_agent
    from .models import AgentRecord
    from .telemetry_models import AgentStatusCurrentRecord
    from .work_models import AssignmentRecord, ExecutionTaskRecord

    active_ids = set((await db.scalars(
        select(AssignmentRecord.agent_id).where(AssignmentRecord.status == "ACTIVE")
    )).all())
    active_ids.update(aid for aid in (await db.scalars(
        select(ExecutionTaskRecord.agent_id).where(ExecutionTaskRecord.status == "RUNNING")
    )).all() if aid)
    statuses = {s.agent_id: s.connectivity for s in (await db.scalars(
        select(AgentStatusCurrentRecord)
    )).all()}
    agents = list((await db.scalars(
        select(AgentRecord).where(AgentRecord.worker_uid.is_not(None)).order_by(AgentRecord.worker_uid)
    )).all())
    # Prefer explicitly healthy workers, then workers whose telemetry is still
    # unknown; dispatch itself remains the final fail-closed bridge check.
    agents.sort(key=lambda a: (0 if statuses.get(a.agent_id) == "HEALTHY" else 1,
                               a.worker_uid or "999"))
    for agent in agents:
        if agent.agent_id in active_ids:
            continue
        if get_bridge_for_agent(agent.agent_id) is not None:
            return agent
    return None


async def _assign_and_dispatch(
    db: AsyncSession, run: JiraReconciliationRunRecord, issue: Any,
    work: WorkItemRecord, link: JiraIssueLinkRecord, counts: ReconcileCounts,
) -> dict[str, Any]:
    from . import work_service
    from .dispatch_service import dispatch_work
    from .work_models import AssignmentCreate

    agent = await _select_idle_worker(db)
    if agent is None:
        return {"outcome": "queued_no_worker", "reason": "no configured idle worker",
                "work_item_id": work.work_item_id}
    assignment, err = await work_service.assign_primary(
        db, AssignmentCreate(agent_id=agent.agent_id, work_item_id=work.work_item_id),
        assigned_by="jira_reconcile",
    )
    if assignment is None:
        return {"outcome": "queued_assignment_failed", "reason": err or "assignment failed",
                "work_item_id": work.work_item_id, "agent_id": agent.agent_id}
    try:
        dispatched = await dispatch_work(
            db, work_item_id=work.work_item_id,
            instruction=_intake_instruction(issue, work.work_uid),
            agent_id=agent.agent_id,
            idempotency_key=f"jira:{issue.key}:generation:{(link.generation_count or 0) + 1}",
            actor=f"jira_reconcile:{run.run_id}",
        )
    except Exception as exc:  # release the primary lock on any failed delivery
        assignment.status = "RELEASED"
        assignment.closed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"outcome": "queued_dispatch_error", "reason": str(exc),
                "work_item_id": work.work_item_id, "agent_id": agent.agent_id}
    if not dispatched.get("dispatched"):
        assignment.status = "RELEASED"
        assignment.closed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"outcome": "queued_dispatch_blocked", "reason": dispatched.get("reason", "blocked"),
                "work_item_id": work.work_item_id, "agent_id": agent.agent_id,
                "task_id": dispatched.get("task_id")}

    now = datetime.now(timezone.utc)
    assignment.dispatched_at = now
    assignment.acknowledged_at = now
    assignment.bound_session_id = dispatched.get("external_task_id")
    link.generation_count = (link.generation_count or 0) + 1
    link.last_ready_observation_at = now
    link.last_generation_started_at = now
    counts.started += 1
    await db.commit()

    # Useful lifecycle signal. Jira transition failures are audited but cannot
    # erase a successfully accepted worker run.
    try:
        client = JiraClient()
        client.add_comment(
            issue.key,
            f"## ACMS execution started\n\n"
            f"- Work: `{work.work_uid or work.work_key}`\n"
            f"- Worker: `{agent.display_name}`\n"
            f"- Assignment: `{assignment.assignment_key or assignment.assignment_id}`\n"
            f"- Runtime task: `{dispatched.get('task_id')}`\n\n"
            "ACMS will post the canonical handoff and exact artifact URL after verified completion.",
        )
        if client.status_mutation_enabled:
            client.transition_status(issue.key, "IN PROGRESS")
    except Exception as exc:  # lifecycle write-back is best-effort, fully audited
        await add_event(
            db, event_type="JIRA_OUTBOUND_FAILED", actor_source="jira_reconcile",
            agent_id=agent.agent_id, work_key=work.work_key,
            summary=f"Jira {issue.key} start write-back failed: {str(exc)[:200]}",
            metadata={"run_id": run.run_id, "issue_key": issue.key,
                      "phase": "started", "error": str(exc)[:400]},
        )
        await db.commit()

    return {"outcome": "started", "reason": "work created, assignment ACKed, runtime accepted",
            "work_item_id": work.work_item_id, "agent_id": agent.agent_id,
            "task_id": dispatched.get("task_id")}


async def _intake_and_start(
    db: AsyncSession, run: JiraReconciliationRunRecord, issue: Any,
    counts: ReconcileCounts,
) -> dict[str, Any]:
    """Create exactly one Jira-backed Work Item, map project, assign, dispatch."""
    from . import work_service
    from .jira_gate import ELIGIBLE
    from .work_models import WorkItemCreate

    issue_id = str(getattr(issue, "issue_id", "") or "")
    if not issue_id:
        return {"outcome": "failed", "reason": "Jira issue has no immutable id"}
    # Race/idempotency guard even though reconciliation runs are leased.
    existing = (await db.scalars(
        select(JiraIssueLinkRecord).where(JiraIssueLinkRecord.jira_issue_id == issue_id)
    )).first()
    if existing is not None:
        return {"outcome": "already_linked", "reason": "issue already has one ACMS intake",
                "work_item_id": existing.work_item_id}

    # Crash-repair guard: UID allocation commits before the issue-link commit.
    # If a process died in that narrow window, repair instead of duplicating.
    orphan = (await db.scalars(
        select(WorkItemRecord).where(
            (WorkItemRecord.jira_issue_id == issue_id)
            | (WorkItemRecord.jira_issue_key == issue.key)
        ).order_by(WorkItemRecord.created_at.asc()).limit(1)
    )).first()
    if orphan is not None:
        repaired = JiraIssueLinkRecord(
            link_id=new_id(), jira_site_id="default",
            work_item_id=orphan.work_item_id,
            jira_issue_id=issue_id, jira_issue_key=issue.key,
            jira_url=issue.url, last_status=issue.status,
            last_status_category=issue.status_category,
            last_assignee_account_id=issue.assignee_account_id,
            last_assignee_name=issue.assignee,
            last_checked_at=datetime.now(timezone.utc),
            last_observation_verdict=VERDICT_ELIGIBLE,
            last_observation_reason="repaired missing issue link",
            created_by="jira_reconcile",
        )
        db.add(repaired)
        await db.commit()
        result = await _assign_and_dispatch(db, run, issue, orphan, repaired, counts)
        result["reason"] = "repaired missing issue link; " + result["reason"]
        return result

    project = await _project_for_issue(db, issue)
    if project is None:
        return {"outcome": "project_unmapped",
                "reason": f"no ACMS project maps Jira key {getattr(issue, 'project_key', None) or issue.key.split('-')[0]}"}

    scope = (
        f"# Jira intake — {issue.key}\n\n"
        f"- **Jira:** [{issue.key}]({issue.url})\n"
        f"- **Issue type:** {getattr(issue, 'issue_type', None) or 'Task'}\n"
        f"- **Priority:** {getattr(issue, 'priority', None) or 'unspecified'}\n"
        f"- **Source status:** {issue.status}\n\n"
        f"## Requested work\n\n{(getattr(issue, 'description', '') or '').strip()}"
    )
    work = await work_service.create_work_item(db, WorkItemCreate(
        title=f"{issue.key}: {issue.summary}"[:255],
        created_by="jira_reconcile", scope_markdown=scope,
        project_id=project.project_id,
    ))
    work.jira_site_id = "default"
    work.jira_issue_id = issue_id
    work.jira_issue_key = issue.key
    work.jira_url = issue.url
    work.jira_last_status = issue.status
    work.jira_last_assignee_account_id = issue.assignee_account_id
    work.jira_last_checked_at = datetime.now(timezone.utc)
    work.jira_eligibility = ELIGIBLE
    work.jira_eligibility_reason = "ready status + dedicated AI account assignment"
    link = JiraIssueLinkRecord(
        link_id=new_id(), jira_site_id="default", jira_issue_id=issue_id,
        jira_issue_key=issue.key, jira_url=issue.url, work_item_id=work.work_item_id,
        created_by="jira_reconcile",
    )
    db.add(link)
    counts.added += 1
    await add_event(
        db, event_type="JIRA_WORK_IMPORTED", actor_source="jira_reconcile",
        work_key=work.work_key,
        summary=f"Jira {issue.key} imported as {work.work_uid or work.work_key}",
        metadata={"run_id": run.run_id, "issue_key": issue.key,
                  "work_item_id": work.work_item_id, "work_uid": work.work_uid,
                  "project_id": project.project_id},
    )
    await db.commit()
    return await _assign_and_dispatch(db, run, issue, work, link, counts)


# ------------------------------------------------------------------ actions

async def _apply_ready(db: AsyncSession, work: WorkItemRecord, link: JiraIssueLinkRecord,
                       run: JiraReconciliationRunRecord, counts: ReconcileCounts,
                       issue: Any, hold_kinds: list[str]) -> None:
    """Ready + AI assignee on a LINKED issue.

    - already active/running → reconcile (no duplicate)
    - unstarted → record the ready observation; START is a separate explicit
      step (start_request) so bootstraps/imports choose their own moment —
      EXCEPT when the work item is PLANNED and the authorization is fresh,
      where §13.2 row 1 says: import/correlate, plan, queue/start once gates
      pass. The gates (budget/holds) are re-verified at dispatch time by
      dispatch_service — so queueing here is safe and idempotent.
    """
    link.last_ready_observation_at = datetime.now(timezone.utc)

    if work.status in ("active",):
        counts.unchanged += 1
        return
    if HOLD_STOP in hold_kinds:
        # stopped generations never restart from a re-polled identical snapshot
        counts.unchanged += 1
        await add_event(db, event_type="JIRA_READY_IGNORED_LOCAL_STOP", actor_source="jira_reconcile",
                        work_key=work.work_key,
                        summary=f"Jira {issue.key} is ready but a local STOP hold exists; "
                                f"explicit operator restart authorization required",
                        metadata={"issue_key": issue.key, "run_id": run.run_id})
        return
    if HOLD_PAUSE in hold_kinds:
        counts.unchanged += 1
        return

    if work.status in ("planned",):
        # The human kickoff contract has passed; Check Jira now is explicitly
        # execution-capable. Route through the one dispatch path immediately.
        result = await _assign_and_dispatch(db, run, issue, work, link, counts)
        if result.get("outcome") != "started":
            counts.unchanged += 1
        await add_event(db, event_type="JIRA_WORK_QUEUED", actor_source="jira_reconcile",
                        work_key=work.work_key,
                        summary=f"Jira {issue.key} ready+assigned → {result.get('outcome')}",
                        metadata={"issue_key": issue.key, "run_id": run.run_id,
                                  "generation": (link.generation_count or 0),
                                  "outcome": result})
    else:
        counts.unchanged += 1


async def _apply_pause(db: AsyncSession, work: WorkItemRecord, link: JiraIssueLinkRecord,
                       run: JiraReconciliationRunRecord, counts: ReconcileCounts, *,
                       reason: str, hold_kind: str = HOLD_PAUSE) -> None:
    """Reversible pause: block dispatch first, then fan out to runtimes (§13.3)."""
    existing = (await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.work_item_id == work.work_item_id)
        .where(WorkRuntimeHoldRecord.cleared_at.is_(None))
        .where(WorkRuntimeHoldRecord.hold_kind == hold_kind)
    )).first()
    if existing is None:
        db.add(WorkRuntimeHoldRecord(
            hold_id=new_id(), work_item_id=work.work_item_id,
            hold_kind=hold_kind, reason=reason[:512],
            created_by=SCHEDULER_ACTOR, source="scheduled_poll",
        ))
    counts.paused += 1
    acknowledged = await _fanout_pause(db, work)
    counts.paused_acknowledged += 1 if acknowledged else 0
    await add_event(db, event_type="JIRA_WORK_PAUSED", actor_source="jira_reconcile",
                    work_key=work.work_key,
                    summary=f"{reason} — pause requested"
                            + ("; runtime acknowledged" if acknowledged else "; runtime NOT yet acknowledged"),
                    metadata={"issue_key": link.jira_issue_key, "run_id": run.run_id,
                              "acknowledged": acknowledged, "hold_kind": hold_kind})
    # Human Inbox (STEA-004 plan §6/§23): BLOCKED/DEFERRED may need a human
    # decision (unblock or defer further). Correlation dedupes repeat polls.
    from .inbox_service import create_inbox_item

    await create_inbox_item(
        db, title=f"Work paused: {work.work_key or work.work_item_id[:8]}",
        summary=f"Jira says {reason.split('→')[-1].strip() or 'paused'}. Work paused. Decide: unblock or keep deferred.",
        item_class="ACTION_REQUIRED", severity="medium",
        agent_id=None, work_item_id=work.work_item_id,
        jira_issue_key=link.jira_issue_key,
        correlation_id=f"jira-pause:{link.jira_issue_key}",
        metadata={"reason": reason[:300], "hold_kind": hold_kind,
                  "runtime_acknowledged": acknowledged})
    await db.commit()


async def _apply_stop(db: AsyncSession, work: WorkItemRecord, link: JiraIssueLinkRecord,
                      run: JiraReconciliationRunRecord, counts: ReconcileCounts, *,
                      reason: str, soft: bool = False) -> None:
    """Stop/cancel generation: invalidate leases, cancel queued, preserve artifacts."""
    db.add(WorkRuntimeHoldRecord(
        hold_id=new_id(), work_item_id=work.work_item_id,
        hold_kind=HOLD_STOP, reason=reason[:512],
        created_by=SCHEDULER_ACTOR, source="scheduled_poll",
    ))
    counts.stopped += 1
    acknowledged = await _fanout_stop(db, work)
    counts.stopped_acknowledged += 1 if acknowledged else 0
    link.generation_count = (link.generation_count or 0) + 1  # terminal — closes the generation
    await add_event(db, event_type="JIRA_WORK_STOPPED", actor_source="jira_reconcile",
                    work_key=work.work_key,
                    summary=f"{reason} — stop requested"
                            + ("; runtime acknowledged" if acknowledged else "; runtime NOT yet acknowledged")
                            + ("; partial output preserved" if soft else ""),
                    metadata={"issue_key": link.jira_issue_key, "run_id": run.run_id,
                              "acknowledged": acknowledged, "soft": soft})
    await db.commit()


async def _fanout_pause(db: AsyncSession, work: WorkItemRecord) -> bool:
    """Cooperative pause via existing runtime controls (ADR-0012 era).

    Uses the runtime API's stop-with-preservation semantics where authorized;
    returns True only on VERIFIED acknowledgment — a still-running worker is
    never labeled paused (§13.3). Failures escalate via Attention events; they
    never block the reconciliation pass.
    """
    try:
        from .runtime_service import request_pause_for_work  # existing control plane

        return bool(await request_pause_for_work(db, work.work_item_id))
    except Exception:
        return False


async def _fanout_stop(db: AsyncSession, work: WorkItemRecord) -> bool:
    try:
        from .runtime_service import request_stop_for_work

        return bool(await request_stop_for_work(db, work.work_item_id))
    except Exception:
        return False


# ------------------------------------------------------------------ observation

async def _observe(db: AsyncSession, issue: Any, link: JiraIssueLinkRecord | None,
                   *, source: str) -> None:
    db.add(JiraIssueObservationRecord(
        observation_id=new_id(),
        work_item_id=link.work_item_id if link else None,
        jira_site_id="default",
        jira_issue_id=str(getattr(issue, "issue_id", "") or ""),
        jira_issue_key=issue.key,
        observed_status=issue.status,
        observed_assignee_account_id=issue.assignee_account_id,
        observed_at=datetime.now(timezone.utc),
        source=source,
        read_outcome="ok",
    ))


async def _observe_failure(db: AsyncSession, link: JiraIssueLinkRecord, error: str) -> None:
    db.add(JiraIssueObservationRecord(
        observation_id=new_id(),
        work_item_id=link.work_item_id,
        jira_site_id="default",
        jira_issue_id=link.jira_issue_id,
        jira_issue_key=link.jira_issue_key,
        observed_status=None,
        observed_assignee_account_id=None,
        observed_at=datetime.now(timezone.utc),
        source="scheduled_poll",
        read_outcome="failed",
        detail_json=json.dumps({"error": error[:400]}),
    ))


# ------------------------------------------------------------------ scheduler

async def _scheduler_state(db: AsyncSession):
    from .jira_models import JiraSyncSchedulerStateRecord

    row = (await db.scalars(select(JiraSyncSchedulerStateRecord))).first()
    if row is None:
        row = JiraSyncSchedulerStateRecord(id=1)
        db.add(row)
    return row


async def due_for_scheduled_poll(db: AsyncSession) -> tuple[bool, dict[str, Any]]:
    """Is the durable poll due? (§13.6: interval ≤ 24h enforced; UTC persisted.)

    Manual polls never postpone the next scheduled run beyond the interval:
    due-ness is computed from the last SUCCESSFUL poll only.
    """
    s_rows = await _scheduler_state(db)
    sched = s_rows
    interval_h = min(max(int(getattr(get_settings(), "jira_reconcile_interval_hours", 24) or 24), 1), 24)
    last_ok = sched.last_successful_poll_at
    last_attempt = sched.last_attempt_at
    now = datetime.now(timezone.utc)
    if last_ok is None:
        return True, {"reason": "never_run", "interval_hours": interval_h,
                      "last_successful_poll_at": None, "last_attempt_at": _iso(last_attempt)}
    next_due = last_ok + timedelta(hours=interval_h)
    return now >= next_due, {
        "reason": "due" if now >= next_due else "not_due",
        "interval_hours": interval_h,
        "last_successful_poll_at": _iso(last_ok),
        "next_due_at": _iso(next_due),
        "last_attempt_at": _iso(last_attempt),
        "overdue": now > next_due + timedelta(hours=interval_h),
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None