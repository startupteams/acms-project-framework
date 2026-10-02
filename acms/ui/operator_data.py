"""Operator UI shared data layer (STEA-004 Phase C / Release 5, plan §18-§29).

Thin read-only aggregation over the Phase-B backend (§33: "Reuse existing APIs
wherever possible" — these functions call the SAME services the JSON APIs use,
never a second derivation). Every page renders ONLY data the backend actually
maintains; missing/stale values render as their true state (—, STALE, NULL),
never fabricated (honesty contract, plan §29 + ADR-0008).

Shared by /ui/ (Home zones), /ui/work/board (Kanban §12), /ui/usage (§17),
and the Product/Project pages (§13/§14/§16) so API and UI cannot drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..a2a_models import (
    ArtifactRecord,
    HumanInboxItemRecord,
    ProductRecord,
)
from ..economics_models import CostAttributionRecord
from ..memory_models import ExecutionSessionRecord
from ..models import AgentRecord
from ..telemetry_models import AgentStatusCurrentRecord
from ..usage_service import usage_summary as usage_session_summary  # re-export (§12.2/§12.3)
from ..work_models import (
    AssignmentRecord,
    ExecutionTaskRecord,
    WorkItemRecord,
)


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime("%m-%d %H:%M") if dt else None


def _age_minutes(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - aware).total_seconds() / 60.0


# ---------------------------------------------------------------- work now


async def work_now(db: AsyncSession) -> dict:
    """Home Zone 1 (§18): running / dispatching / blocked / in-review /
    recent completions — each a direct query over real rows."""
    running = (await db.scalars(
        select(ExecutionTaskRecord)
        .where(ExecutionTaskRecord.status == "RUNNING",
               ExecutionTaskRecord.transport_state == "RUNNING")
        .order_by(ExecutionTaskRecord.started_at.desc())
    )).all()
    dispatching = (await db.scalars(
        select(ExecutionTaskRecord)
        .where(ExecutionTaskRecord.status == "RUNNING",
               ExecutionTaskRecord.transport_state == "DISPATCHING")
        .order_by(ExecutionTaskRecord.started_at.desc())
    )).all()
    blocked_items = (await db.scalars(
        select(WorkItemRecord)
        .where(WorkItemRecord.status == "blocked")
        .order_by(WorkItemRecord.updated_at.desc()).limit(10)
    )).all()
    review_items = (await db.scalars(
        select(WorkItemRecord)
        .where(WorkItemRecord.status == "in_review")
        .order_by(WorkItemRecord.updated_at.desc()).limit(10)
    )).all()
    recent_done = (await db.scalars(
        select(ExecutionTaskRecord)
        .where(ExecutionTaskRecord.status.in_(("SUCCEEDED", "FAILED")))
        .order_by(ExecutionTaskRecord.finished_at.desc()).limit(5)
    )).all()

    # titles for tasks
    wid_set = {t.work_item_id for t in running} | {t.work_item_id for t in dispatching} \
        | {t.work_item_id for t in recent_done}
    titles: dict[str, str] = {}
    if wid_set:
        for r in (await db.scalars(
                select(WorkItemRecord.work_item_id, WorkItemRecord.title,
                       WorkItemRecord.work_uid)
                .where(WorkItemRecord.work_item_id.in_(wid_set)))).all():
            titles[r[0]] = r[2] or r[1]

    def _task(t: ExecutionTaskRecord) -> dict:
        return {
            "task_id": t.task_id,
            "work_item_id": t.work_item_id,
            "work_label": titles.get(t.work_item_id, (t.work_item_id or "")[:8]),
            "status": t.status,
            "transport_state": t.transport_state,
            "model": t.effective_model,
            "started_str": _fmt_dt(t.started_at),
            "finished_str": _fmt_dt(t.finished_at),
        }

    return {
        "running": [_task(t) for t in running],
        "dispatching": [_task(t) for t in dispatching],
        "blocked": [{"work_item_id": w.work_item_id, "work_uid": w.work_uid or w.work_key,
                     "title": w.title} for w in blocked_items],
        "in_review": [{"work_item_id": w.work_item_id, "work_uid": w.work_uid or w.work_key,
                       "title": w.title} for w in review_items],
        "recent": [_task(t) for t in recent_done],
    }


# ---------------------------------------------------------------- fleet


async def fleet_rows(db: AsyncSession) -> list[dict]:
    """Agent Fleet rows (§19): durable human name prominent, ACMS UUID
    secondary; runtime facts from Server Manager (node/VMID) when available."""
    from ..registry import list_agents

    agents = await list_agents(db)
    status_rows = {r.agent_id: r for r in (
        await db.scalars(select(AgentStatusCurrentRecord))).all()}

    # runtime facts (node/VMID) via Server Manager — display-only, optional
    runtime_by_agent: dict[str, dict] = {}
    from ..settings import get_settings

    s = get_settings()
    if s.server_manager_base_url and s.server_manager_token:
        from ..server_manager_client import ServerManagerClient

        try:
            client = ServerManagerClient(s.server_manager_base_url, s.server_manager_token)
            for rt in client.list_runtimes():
                runtime_by_agent[rt.acms_agent_id] = {
                    "node": rt.node, "vmid": rt.vmid,
                    "actual_state": rt.raw.get("actual_state"),
                }
        except Exception:  # noqa: BLE001 — display-only; page must render without SM
            runtime_by_agent = {}

    # today's token/cost per agent — from execution sessions (agent-authoritative)
    from .session_usage import agent_usage_today

    usage = await agent_usage_today(db)

    out = []
    for a in agents:
        st = status_rows.get(a.agent_id)
        conn = (st.connectivity if st else None) or "UNKNOWN"
        running_flag = (st.agent_running if st else None) or "unknown"
        if conn == "STALE":
            state = "STALE"
        elif conn == "UNREACHABLE":
            state = "OFFLINE"
        elif running_flag == "true":
            state = "RUNNING"
        else:
            state = "IDLE"
        rt = runtime_by_agent.get(a.agent_id) or {}
        u = usage.get(a.agent_id) or {}
        out.append({
            "agent_id": a.agent_id,
            "name": a.display_name,               # durable human name (§20 primary)
            "worker_uid": a.worker_uid,
            "legacy_name": a.legacy_name,
            "harness": a.harness,
            "trust_class": a.trust_class.value,
            "state": state,
            "connectivity": conn,
            "last_contact_str": _fmt_dt(st.last_contact_at if st else None),
            "model": st.model_id if st else None,
            "session_title": st.session_title if st else None,
            "node": rt.get("node"), "vmid": rt.get("vmid"),
            "tokens_today": u.get("tokens"),
            "cost_today": u.get("cost"),
        })
    return out


# ---------------------------------------------------------------- attention


async def human_attention(db: AsyncSession) -> dict:
    """Home Zone 3 (§18) — unread inbox counts + oldest waiting item."""
    unread_action = (await db.scalar(
        select(func.count()).select_from(HumanInboxItemRecord)
        .where(HumanInboxItemRecord.item_class == "ACTION_REQUIRED",
               HumanInboxItemRecord.archived_at.is_(None),
               HumanInboxItemRecord.read_at.is_(None)))) or 0
    unread_fyi = (await db.scalar(
        select(func.count()).select_from(HumanInboxItemRecord)
        .where(HumanInboxItemRecord.item_class == "FYI",
               HumanInboxItemRecord.archived_at.is_(None),
               HumanInboxItemRecord.read_at.is_(None)))) or 0
    oldest = (await db.scalars(
        select(HumanInboxItemRecord)
        .where(HumanInboxItemRecord.archived_at.is_(None),
               HumanInboxItemRecord.read_at.is_(None))
        .order_by(HumanInboxItemRecord.created_at.asc()).limit(1))).first()
    return {
        "unread_action": unread_action,
        "unread_fyi": unread_fyi,
        "oldest": {
            "item_id": oldest.item_id, "title": oldest.title,
            "item_class": oldest.item_class,
            "age_str": _fmt_dt(oldest.created_at),
        } if oldest else None,
    }


# ---------------------------------------------------------------- system/cost health


async def system_cost_health(db: AsyncSession) -> dict:
    """Home Zone 4 (§18): DB health, cloud cost today, facility power.
    Stale/absent upstream → STALE/—, NEVER 0 (§17)."""
    from ..power_ingest import latest_power_summary

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cloud_today = (await db.scalar(
        select(func.sum(func.coalesce(CostAttributionRecord.api_cost_usd, 0.0)))
        .where(CostAttributionRecord.created_at >= day_start))) or 0.0
    sessions_today = (await db.scalar(
        select(func.count()).select_from(ExecutionSessionRecord)
        .where(ExecutionSessionRecord.started_at >= day_start))) or 0
    power = await latest_power_summary(db)
    return {
        "cloud_cost_today": round(float(cloud_today), 6),
        "sessions_today": sessions_today,
        "power": power,
    }


# ---------------------------------------------------------------- slop cards


async def product_slop_cards(db: AsyncSession) -> list[dict]:
    """Home Product cards (§18): one row per product with its Slop Ratio."""
    from ..slop_metrics import product_slop_summary

    products = (await db.scalars(select(ProductRecord).order_by(ProductRecord.name))).all()
    cards = []
    for p in products:
        s = await product_slop_summary(db, p.product_id)
        cards.append({
            "product_id": p.product_id,
            "name": p.name,
            "has_metrics": bool(s),
            "true_slop_ratio": (s or {}).get("true_slop_ratio"),
            "precision_slop_ratio": (s or {}).get("precision_slop_ratio"),
            "loc": (s or {}).get("loc"),
            "human_adr": (s or {}).get("human_written_adr_count"),
            "total_adr": (s or {}).get("total_adr_count"),
            "repo_count": (s or {}).get("repository_count") or 0,
            "last_measured_str": _fmt_dt((s or {}).get("last_measured_at")),
        })
    return cards


# ---------------------------------------------------------------- kanban (§12)


KANBAN_COLUMNS = [
    ("planned", "Planned"),
    ("dispatching", "Dispatching"),
    ("running", "Running"),
    ("attention", "Human attention / blocked"),
    ("in_review", "In review"),
    ("terminal", "Complete / failed"),
]


async def kanban_data(db: AsyncSession) -> dict:
    """Work Kanban (plan §12) over real rows.

    Column placement is DERIVED from durable state:
      dispatching/running ← execution_tasks.transport_state
      attention ← work_items.status='blocked' OR an un-cleared hold OR
                  Jira eligibility LOCAL_HOLD
      terminal ← task status SUCCEEDED/FAILED/CANCELLED (or work completed/cancelled)
    Jira state stays SEPARATE from ACMS state on every card (§11).
    Cards carry Work UID as primary link text (§12).
    Old TEST/PROOF/ARCHIVED items are labeled (§12 anti-impersonation).
    """
    items = (await db.scalars(
        select(WorkItemRecord).order_by(WorkItemRecord.updated_at.desc()).limit(300))).all()
    tasks = (await db.scalars(
        select(ExecutionTaskRecord).order_by(ExecutionTaskRecord.started_at.desc()).limit(500))).all()

    tasks_by_item: dict[str, list[ExecutionTaskRecord]] = {}
    for t in tasks:
        tasks_by_item.setdefault(t.work_item_id, []).append(t)

    # active holds (window-5 §13.3)
    from ..jira_models import WorkRuntimeHoldRecord

    holds = (await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.cleared_at.is_(None)))).all()
    held_items = {h.work_item_id for h in holds}

    # active assignment → agent name
    assignments = (await db.scalars(
        select(AssignmentRecord).where(AssignmentRecord.status == "ACTIVE"))).all()
    agent_by_item: dict[str, str] = {}
    agent_ids = {a.agent_id for a in assignments}
    names: dict[str, str] = {}
    if agent_ids:
        for r in (await db.scalars(
                select(AgentRecord.agent_id, AgentRecord.display_name)
                .where(AgentRecord.agent_id.in_(agent_ids)))).all():
            names[r[0]] = r[1]
    for a in assignments:
        agent_by_item[a.work_item_id] = names.get(a.agent_id, a.agent_id[:8])

    # artifact counts per item
    art_counts: dict[str, int] = dict((await db.execute(
        select(ArtifactRecord.work_item_id, func.count())
        .where(ArtifactRecord.work_item_id.isnot(None))
        .group_by(ArtifactRecord.work_item_id))).all())

    columns: dict[str, list[dict]] = {c[0]: [] for c in KANBAN_COLUMNS}
    for it in items:
        wid = it.work_item_id
        item_tasks = tasks_by_item.get(wid, [])
        transport = next((t.transport_state for t in item_tasks
                          if t.status == "RUNNING" and t.transport_state), None)
        terminal_task = next((t for t in item_tasks
                              if t.status in ("SUCCEEDED", "FAILED", "CANCELLED")), None)
        if it.status in ("completed", "cancelled"):
            col = "terminal"
        elif it.status == "blocked" or wid in held_items \
                or it.jira_eligibility == "LOCAL_HOLD":
            col = "attention"
        elif transport == "RUNNING":
            col = "running"
        elif transport == "DISPATCHING":
            col = "dispatching"
        elif it.status == "in_review":
            col = "in_review"
        else:
            col = "planned"
        test_label = None
        tl = it.title.upper()
        if "[CANARY" in tl or "TEST" in tl.split() or "PROOF" in tl.split() \
                or "ARCHIVED" in tl:
            test_label = "TEST/PROOF"
        columns[col].append({
            "work_item_id": wid,
            "work_uid": it.work_uid or it.work_key or wid[:8],
            "title": it.title,
            "kind": it.kind,
            "test_label": test_label,
            "jira_issue_key": it.jira_issue_key,
            "jira_url": it.jira_url,
            "jira_last_status": it.jira_last_status,
            "jira_eligibility": it.jira_eligibility,
            "acms_status": it.status,
            "agent_name": agent_by_item.get(wid),
            "model": next((t.effective_model for t in item_tasks if t.effective_model), None),
            "transport": transport,
            "cost_known": bool(terminal_task),   # cost card links to detail rollup
            "artifact_count": art_counts.get(wid, 0),
            "updated_str": _fmt_dt(it.updated_at),
        })

    return {
        "columns": [
            {"status": key, "label": label, "cards": columns.get(key, [])}
            for key, label in KANBAN_COLUMNS
        ],
        "total": len(items),
    }


# ---------------------------------------------------------------- usage page (§17)


async def usage_page_data(db: AsyncSession, hours: int = 24) -> dict:
    """Usage/cost page (§17): facility electricity cards + token/cost rollups.
    Power comes from the LATEST SNAPSHOT (no fetch in the request path —
    refresh=1 stays an explicit action on the JSON API + UI button)."""
    from ..power_ingest import latest_power_summary, usage_summary

    power = await latest_power_summary(db)
    usage = await usage_summary(db, hours)
    by_provider: dict[str, dict] = {}
    for m in usage.get("models", []):
        b = by_provider.setdefault(m["provider"], {"requests": 0, "input_tokens": 0,
                                                   "output_tokens": 0, "cost": 0.0,
                                                   "models": 0})
        b["requests"] += m["requests"]
        b["input_tokens"] += m["input_tokens"]
        b["output_tokens"] += m["output_tokens"]
        b["cost"] += m["cloud_cost_usd_actual"]
        b["models"] += 1
    return {"power": power, "usage": usage, "by_provider": by_provider,
            "hours": hours}