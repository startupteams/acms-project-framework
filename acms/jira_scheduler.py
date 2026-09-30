"""Durable Jira reconciliation scheduler (window-5 plan §13.6).

Runs INSIDE the app process (like telemetry_scheduler + run_watcher — the
documented single-replica discipline, TDR-0009; multi-replica deployments use
the durable lease in jira_reconciliation_runs + leader election later).

- Interval: settings.jira_reconcile_interval_hours, CLAMPED to ≤ 24 (plan:
  "validate that configuration cannot exceed 24 hours").
- UTC persisted via jira_scheduler_state.
- Catch-up: one poll immediately at startup when overdue; missed runs coalesce
  (never replay a burst).
- Manual runs never postpone the scheduled cadence beyond the interval (due-ness
  computed from last SUCCESSFUL poll only — jira_reconcile.due_for_scheduled_poll).
- Overdue/missed-run degradation: JIRA_RECONCILE_OVERDUE events (Attention picks
  them from the audit log) — outages are REPORTED, never marked successful.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .settings import get_settings
from .telemetry_service import add_event

_POLL_SECONDS_CHECK = 300  # how often the loop checks due-ness
_started = False
_task: asyncio.Task | None = None


def _interval_seconds() -> int:
    hours = min(max(int(getattr(get_settings(), "jira_reconcile_interval_hours", 24) or 24), 1), 24)
    return hours * 3600


async def _poll_once(trigger: str) -> dict | None:
    """One reconciliation pass if due. Returns the outcome dict (or None)."""
    from .db import SessionLocal
    from .jira_reconcile import due_for_scheduled_poll, execute_reconciliation, start_reconciliation_run

    async with SessionLocal() as db:
        due, info = await due_for_scheduled_poll(db)
        if not due:
            return None
        outcome = await start_reconciliation_run(db, trigger=trigger,
                                                 requested_by="jira-reconcile-scheduler")
        # record freshness
        from .jira_models import JiraSyncSchedulerStateRecord

        sched = (await db.scalars(select(JiraSyncSchedulerStateRecord))).first()
        if sched is None:
            sched = JiraSyncSchedulerStateRecord(id=1)
            db.add(sched)
        sched.last_run_id = outcome.run_id
        interval = min(max(int(getattr(get_settings(), "jira_reconcile_interval_hours", 24) or 24), 1), 24)
        sched.next_due_at = datetime.now(timezone.utc) + timedelta(hours=interval)
        sched.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return {"run_id": outcome.run_id, "state": outcome.state, "due_info": info}


async def _loop() -> None:
    """Durable poll loop. Survives transient DB/Jira failures (bounded backoff)."""
    consecutive_failures = 0
    while True:
        try:
            outcome = await _poll_once("scheduled_poll")
            consecutive_failures = 0
            if outcome:
                # a poll ran; emit nothing extra (run events already durable)
                pass
        except Exception as e:  # noqa: BLE001 — the loop must survive outages
            consecutive_failures += 1
            backoff = min(30 * consecutive_failures, 600)
            try:
                from .db import SessionLocal

                async with SessionLocal() as db:
                    await add_event(
                        db, event_type="JIRA_RECONCILE_FAILED", actor_source="jira_reconcile",
                        summary=f"scheduled poll error ({str(e)[:200]}); retry in {backoff}s",
                        metadata={"consecutive_failures": consecutive_failures},
                    )
                    await db.commit()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(backoff)
            continue
        await asyncio.sleep(_POLL_SECONDS_CHECK)


async def _overdue_watchdog() -> None:
    """Detect overdue/missed polls and mark degraded health (§13.6)."""
    from .db import SessionLocal
    from .jira_reconcile import due_for_scheduled_poll

    last_overdue_emitted: datetime | None = None
    while True:
        try:
            async with SessionLocal() as db:
                due, info = await due_for_scheduled_poll(db)
                if due and info.get("overdue"):
                    now = datetime.now(timezone.utc)
                    if last_overdue_emitted is None or (now - last_overdue_emitted) > timedelta(hours=1):
                        await add_event(
                            db, event_type="JIRA_RECONCILE_OVERDUE", actor_source="jira_reconcile",
                            summary=f"no fully successful Jira reconciliation within the interval "
                                    f"({info.get('interval_hours')}h) — degraded sync health; "
                                    f"Jira-only changes may take up to the polling interval to detect",
                            metadata=info,
                        )
                        await db.commit()
                        last_overdue_emitted = now
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1800)


async def startup_catchup() -> None:
    """One immediate poll at startup when overdue (§13.6); coalesces missed runs."""
    try:
        from .db import SessionLocal
        from .jira_reconcile import due_for_scheduled_poll, start_reconciliation_run

        async with SessionLocal() as db:
            due, info = await due_for_scheduled_poll(db)
            if due:
                await start_reconciliation_run(db, trigger="startup_catchup",
                                               requested_by="jira-reconcile-scheduler")
    except Exception:  # noqa: BLE001 — startup must never crash on the poll
        pass


def start_scheduler() -> None:
    """Idempotent startup hook (called from app startup event)."""
    global _started, _task
    if _started:
        return
    if not get_settings().jira_reconcile_enabled:
        return
    _started = True
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_loop())
    loop.create_task(_overdue_watchdog())
    loop.create_task(startup_catchup())


def scheduler_running() -> bool:
    return _started and _task is not None and not _task.done()