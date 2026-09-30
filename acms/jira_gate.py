"""Jira kickoff eligibility gate (window-5 plan §7/§13).

The human-facing planning layer is Jira. A Work Item may be dispatched to AI
execution ONLY when:

  1. it carries a Jira linkage (site + immutable issue id + key), and
  2. the linked issue's CURRENT observed status is a configured READY state
     (default ``TO START`` — project workflow metadata is verified at runtime,
     never assumed globally valid), and
  3. the issue is CURRENTLY assigned to the dedicated AI account
     (startupteamscompany@gmail.com — resolved to a Jira accountId, matched by
     accountId, never by display name or email visibility), and
  4. no local hold (pause/stop) or operator restart authorization is pending.

Eligibility is NECESSARY but never sufficient: budgets, runtime health, and
local holds still apply after this gate (dispatch order: Jira → budget → …).

Server-side enforcement: dispatch_service calls :func:`gate_work_item` on every
path (UI, API, scheduler, retry, resume, Executive decomposition, product
bootstrap). Drafts may be saved unlinked; they are visibly non-executable.

Design notes:
- The gate NEVER mutates Jira. Observation is read-only; transitions are the
  reconciliation service's concern.
- Local holds are durable DB rows — a process restart cannot clear them and a
  poll cannot override them (plan §13.3).
- One Jira issue may authorize multiple ACMS Work Items (§7.3) — descendants
  inherit the authorization; the gate checks the TOP-LEVEL linkage up the
  parent chain as well as the item's own linkage.
- Observation staleness: eligibility is verified at dispatch time with a LIVE
  Jira read when credentials are configured (bounded timeout). If Jira is
  unreachable the gate returns an UNKNOWN verdict — new dispatch is inhibited
  (fail-closed), and running work keeps going (§13.2 last row).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .jira_models import new_id
from .settings import get_settings
from .work_models import WorkItemRecord

# --- verdict vocabulary (persisted; the UI renders these verbatim) -----------
ELIGIBLE = "ELIGIBLE"
NOT_LINKED = "NOT_LINKED"
NOT_READY = "NOT_READY"
ASSIGNEE_MISMATCH = "ASSIGNEE_MISMATCH"
LOCAL_HOLD = "LOCAL_HOLD"
UNKNOWN = "JIRA_UNKNOWN"
STALE_AUTHORIZATION = "STALE_AUTHORIZATION"

# Local hold kinds (plan §13.3: pause/stop are persistent local controls)
HOLD_PAUSE = "PAUSE"
HOLD_STOP = "STOP"


@dataclass
class GateVerdict:
    eligible: bool
    verdict: str
    reason: str
    observed_status: str | None = None
    observed_assignee_account_id: str | None = None
    checked_at: float = field(default_factory=time.time)
    jira_issue_key: str | None = None
    jira_url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "verdict": self.verdict,
            "reason": self.reason,
            "observed_status": self.observed_status,
            "observed_assignee_account_id": self.observed_assignee_account_id,
            "checked_at": self.checked_at,
            "jira_issue_key": self.jira_issue_key,
            "jira_url": self.jira_url,
            **({"details": self.details} if self.details else {}),
        }


class JiraGateError(Exception):
    """Structural gate failure (bad config, malformed linkage)."""


def ready_statuses() -> set[str]:
    """Configured READY states (uppercase compare; default {TO START})."""
    s = get_settings()
    raw = getattr(s, "jira_ready_statuses", "") or "TO START"
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


def ai_account_id_setting() -> str:
    """Configured dedicated-AI Jira accountId ('' = not pinned)."""
    return (getattr(get_settings(), "jira_ai_account_id", "") or "").strip()


def _issue_status_upper(status_name: str | None) -> str:
    return (status_name or "").strip().upper()


def evaluate_observation(
    *, status_name: str | None, assignee_account_id: str | None,
    has_local_hold: bool, hold_kinds: list[str] | None = None,
) -> GateVerdict:
    """Pure mapping of one Jira observation (+local holds) to a GateVerdict.

    §13.2 precedence: local stop/pause > assignment mismatch > non-ready state;
    an eligible snapshot never overrides budget/hold checks performed later.
    """
    s = get_settings()
    ai_id = ai_account_id_setting()

    if has_local_hold:
        kinds = hold_kinds or []
        kind = HOLD_STOP if HOLD_STOP in kinds else HOLD_PAUSE
        return GateVerdict(
            eligible=False, verdict=LOCAL_HOLD,
            reason=f"local {kind.lower()} hold persists; an operator must explicitly clear it "
                   f"before dispatch (polls cannot override it)",
            observed_status=status_name, observed_assignee_account_id=assignee_account_id,
        )

    if ai_id and (assignee_account_id or "") != ai_id:
        return GateVerdict(
            eligible=False, verdict=ASSIGNEE_MISMATCH,
            reason="issue is not assigned to the dedicated AI account "
                   f"(expected accountId {ai_id[:14]}…; observed "
                   f"{(assignee_account_id or 'unassigned')[:14]}…)",
            observed_status=status_name, observed_assignee_account_id=assignee_account_id,
        )
    if not ai_id:
        # No pinned account: fail-closed — an unpinned AI account is a config
        # gap, not an eligibility pass (never infer from email visibility).
        return GateVerdict(
            eligible=False, verdict=UNKNOWN,
            reason="ACMS_JIRA_AI_ACCOUNT_ID is not configured; eligibility cannot be verified",
            observed_status=status_name, observed_assignee_account_id=assignee_account_id,
        )

    status_u = _issue_status_upper(status_name)
    if status_u not in ready_statuses():
        return GateVerdict(
            eligible=False, verdict=NOT_READY,
            reason=f"issue status '{status_name}' is not a configured ready state "
                   f"({sorted(ready_statuses())}); set it in Jira "
                   f"(e.g. move to '{sorted(ready_statuses())[0] if ready_statuses() else 'TO START'}') "
                   f"and keep it assigned to the AI account",
            observed_status=status_name, observed_assignee_account_id=assignee_account_id,
        )

    return GateVerdict(
        eligible=True, verdict=ELIGIBLE,
        reason="issue is in a ready state and assigned to the dedicated AI account",
        observed_status=status_name, observed_assignee_account_id=assignee_account_id,
    )


async def _own_linkage(work: WorkItemRecord) -> dict[str, Any] | None:
    """The item's own persisted Jira linkage (if any)."""
    if getattr(work, "jira_issue_id", None):
        return {
            "issue_id": work.jira_issue_id,
            "issue_key": work.jira_issue_key,
            "url": getattr(work, "jira_url", None),
            "site_id": getattr(work, "jira_site_id", None),
        }
    return None


async def _top_level_linkage(db: AsyncSession, work: WorkItemRecord) -> dict[str, Any] | None:
    """§7.3: descendants inherit the authorization link — walk up the parent
    chain (bounded) to find the closest persisted Jira linkage."""
    seen: set[str] = set()
    current: WorkItemRecord | None = work
    depth = 0
    while current is not None and depth < 12:
        link = await _own_linkage(current)
        if link:
            return link
        parent_id = current.parent_id
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        current = await db.get(WorkItemRecord, parent_id)
        depth += 1
    return None


async def _local_holds(db: AsyncSession, work_item_id: str) -> list[dict[str, Any]]:
    """Active local holds for this work item (any kind, unacknowledged-clear)."""
    from .jira_models import WorkRuntimeHoldRecord  # local import avoids cycles

    rows = (await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.work_item_id == work_item_id)
        .where(WorkRuntimeHoldRecord.cleared_at.is_(None))
        .order_by(WorkRuntimeHoldRecord.created_at.desc())
    )).all()
    return [
        {"kind": r.hold_kind, "reason": r.reason, "created_by": r.created_by,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]


async def gate_work_item(db: AsyncSession, work_item_id: str, *,
                         live_check: bool = True) -> GateVerdict:
    """THE server-side Jira eligibility gate.

    Called by dispatch_service on EVERY dispatch path before budget checks.
    Returns the GateVerdict; raises JiraGateError only for structural failures.

    live_check=True performs a bounded live Jira read of the linked issue
    (re-read immediately before the decision — §13.4 step 5). When Jira is
    unreachable the verdict is UNKNOWN (fail-closed for NEW dispatch).
    """
    work = await db.get(WorkItemRecord, work_item_id)
    if work is None:
        raise JiraGateError(f"unknown work item {work_item_id}")

    holds = await _local_holds(db, work_item_id)
    link = await _own_linkage(work) or await _top_level_linkage(db, work)

    if holds and link is None:
        # Operator holds surface even without linkage — the most actionable
        # fact for the human (§13.3: holds persist until explicitly cleared).
        verdict = GateVerdict(
            eligible=False, verdict=LOCAL_HOLD,
            reason=f"local hold active ({', '.join(h['kind'] for h in holds)}); an operator must "
                   f"explicitly clear it; Jira linkage is also missing",
        )
        await _persist_verdict(db, work_item_id, verdict)
        return verdict

    if link is None:
        verdict = GateVerdict(
            eligible=False, verdict=NOT_LINKED,
            reason="no Jira linkage: link (or create) the high-level Jira issue and have a human "
                   "set it to a ready state assigned to the dedicated AI account; "
                   "drafts stay visibly non-executable",
        )
    else:
        verdict = GateVerdict(eligible=False, verdict=UNKNOWN,
                              reason="eligibility not yet observed",
                              jira_issue_key=link.get("issue_key"),
                              jira_url=link.get("url"))
        if live_check:
            from .jira_client import JiraClient, JiraError

            try:
                client = JiraClient()
                issue = client.get_issue_by_id(link["issue_id"]) if link.get("issue_id") \
                    else client.get_issue(link["issue_key"])
                verdict = evaluate_observation(
                    status_name=issue.status,
                    assignee_account_id=issue.assignee_account_id,
                    has_local_hold=bool(holds),
                    hold_kinds=[h["kind"] for h in holds],
                )
                verdict.jira_issue_key = issue.key
                verdict.jira_url = issue.url
                # persist the observation for the work item
                await _record_observation(db, work_item_id, issue)
            except JiraError as e:
                verdict = GateVerdict(
                    eligible=False, verdict=UNKNOWN,
                    reason=f"Jira read failed ({str(e)[:160]}); new dispatch inhibited "
                           f"until Jira is reachable again",
                    jira_issue_key=link.get("issue_key"), jira_url=link.get("url"),
                )
            except Exception as e:  # noqa: BLE001 — connection/timeout classes too
                verdict = GateVerdict(
                    eligible=False, verdict=UNKNOWN,
                    reason=f"Jira read failed ({type(e).__name__}: {str(e)[:140]}); "
                           f"new dispatch inhibited until Jira is reachable again",
                    jira_issue_key=link.get("issue_key"), jira_url=link.get("url"),
                )
        else:
            # Last-persisted observation path (used by read-only UI views)
            last = await _last_observation(db, work_item_id)
            if last is None:
                verdict = GateVerdict(
                    eligible=False, verdict=UNKNOWN,
                    reason="no Jira observation recorded yet; use Check Jira now",
                    jira_issue_key=link.get("issue_key"), jira_url=link.get("url"),
                )
            else:
                verdict = evaluate_observation(
                    status_name=last.get("status"),
                    assignee_account_id=last.get("assignee_account_id"),
                    has_local_hold=bool(holds),
                    hold_kinds=[h["kind"] for h in holds],
                )
                verdict.jira_issue_key = link.get("issue_key")
                verdict.jira_url = link.get("url")

    if holds and verdict.eligible:
        # holds checked inside evaluate_observation for live path; belt-and-braces
        verdict = GateVerdict(
            eligible=False, verdict=LOCAL_HOLD,
            reason="local hold active; operator must clear it",
            observed_status=verdict.observed_status,
            observed_assignee_account_id=verdict.observed_assignee_account_id,
            jira_issue_key=verdict.jira_issue_key, jira_url=verdict.jira_url,
        )

    await _persist_verdict(db, work_item_id, verdict)
    return verdict


# ------------------------------------------------------------------ persistence

async def _record_observation(db: AsyncSession, work_item_id: str, issue: Any) -> None:
    from .jira_models import JiraIssueObservationRecord

    db.add(JiraIssueObservationRecord(
        observation_id=new_id(),
        work_item_id=work_item_id,
        jira_site_id="default",
        jira_issue_id=getattr(issue, "issue_id", None) or "",
        jira_issue_key=issue.key,
        observed_status=issue.status,
        observed_assignee_account_id=issue.assignee_account_id,
        observed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        source="dispatch_gate",
    ))


async def _last_observation(db: AsyncSession, work_item_id: str) -> dict[str, Any] | None:
    from .jira_models import JiraIssueObservationRecord

    row = (await db.scalars(
        select(JiraIssueObservationRecord)
        .where(JiraIssueObservationRecord.work_item_id == work_item_id)
        .order_by(JiraIssueObservationRecord.observed_at.desc())
        .limit(1)
    )).first()
    if row is None:
        return None
    return {"status": row.observed_status,
            "assignee_account_id": row.observed_assignee_account_id,
            "observed_at": row.observed_at.isoformat() if row.observed_at else None}


async def _persist_verdict(db: AsyncSession, work_item_id: str, verdict: GateVerdict) -> None:
    work = await db.get(WorkItemRecord, work_item_id)
    if work is None:
        return
    work.jira_eligibility = verdict.verdict
    work.jira_eligibility_reason = verdict.reason[:512]
    work.jira_last_checked_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    if verdict.observed_status is not None:
        work.jira_last_status = verdict.observed_status
    if verdict.observed_assignee_account_id is not None:
        work.jira_last_assignee_account_id = verdict.observed_assignee_account_id
    # NOTE: no commit here — the CALLER owns the transaction boundary.