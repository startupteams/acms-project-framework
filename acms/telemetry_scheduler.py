"""Background scheduling for live telemetry (combined slice 3+4; ADR-0010; plan §16).

Lightweight in-process loop — no Celery/K8s/event bus. Runs inside the FastAPI
lifespan; every DB operation gets its own session so the loop never holds a
request-scoped session. Time is injected (``now_fn``) so tests never wait real
minutes/hours (plan §20).

Jobs:
- stale evaluation (every tick): derive UNKNOWN/HEALTHY/STALE/UNREACHABLE
- 1-hour stale reconciliation: actively request a full status snapshot
- 24 h fleet reconciliation (staggered tolerance documented in ADR-0010)
- telemetry sampling every ACMS_TELEMETRY_SAMPLE_SECONDS + on material change

Reconciliation authority (plan §9): correct operational telemetry, preserve
history, log discrepancy events; never touch approved scope/assignment.
Multi-replica note: a single ACMS instance is assumed; replicas would need
leader/lock coordination (documented in ADR-0010 + FUTURE_WORK).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal
from .telemetry_models import (
    AgentEventRecord,
    AgentStatusCurrentRecord,
    AgentTelemetrySampleRecord,
    CONNECTIVITY_HEALTHY,
    CONNECTIVITY_STALE,
    CONNECTIVITY_UNREACHABLE,
    CONTACT_SOURCE_RECONCILIATION,
)
from .telemetry_service import TelemetryService, _new_id, add_event, _now
from .settings import get_settings

logger = logging.getLogger("acms.telemetry.scheduler")


class TelemetryScheduler:
    def __init__(self, session_factory=None, now_fn=None):
        self._session_factory = session_factory or SessionLocal
        self._now_fn = now_fn or _now
        self._task: asyncio.Task | None = None
        self._last_fleet_reconcile: datetime | None = None
        self._last_result_reconcile: datetime | None = None
        self._last_sample: dict[str, datetime] = {}
        # C4 state-entry dedupe: AGENT_RUNNING_WITHOUT_ASSIGNMENT is emitted once
        # per state entry (never per tick). Key = agent_id, value = currently in state.
        self._running_unassigned: dict[str, bool] = {}

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="acms-telemetry-scheduler")
    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        from .settings import get_settings

        while True:
            try:
                s = get_settings()
                await self.tick(s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — scheduler must survive transient DB issues
                logger.exception("telemetry scheduler tick failed")
            await asyncio.sleep(5)
    async def tick(self, s) -> None:
        """One scheduler pass. Public so tests can drive time deterministically."""
        now_dt = self._now_fn()
        async with self._session_factory() as db:
            statuses = await TelemetryService.list_status(db)
            for status in statuses:
                await self._evaluate_agent(db, s, status, now_dt)
            await self._maybe_fleet_reconcile(db, s, now_dt)
            await self._reconcile_execution_results(db, s, now_dt)

    # ------------------------------------------------------- per-agent logic

    async def _evaluate_agent(self, db: AsyncSession, s, status: AgentStatusCurrentRecord, now_dt: datetime) -> None:
        agent_id = status.agent_id
        if status.last_contact_at is None:
            return  # never contacted: stays UNKNOWN

        stale_after = timedelta(seconds=s.heartbeat_stale_seconds)
        reconcile_after = timedelta(seconds=s.stale_reconcile_seconds)
        lc = status.last_contact_at
        if lc.tzinfo is None:
            from datetime import timezone as _tz
            lc = lc.replace(tzinfo=_tz.utc)
        delta = now_dt - lc

        new_connectivity = status.connectivity
        if delta <= stale_after:
            new_connectivity = CONNECTIVITY_HEALTHY
        elif delta >= reconcile_after:
            ok = await self._attempt_reconciliation(db, s, status, now_dt)
            new_connectivity = CONNECTIVITY_HEALTHY if ok else CONNECTIVITY_UNREACHABLE
        else:
            new_connectivity = CONNECTIVITY_STALE

        if new_connectivity != status.connectivity:
            prev = status.connectivity
            status.connectivity = new_connectivity
            if new_connectivity == CONNECTIVITY_STALE and status.stale_since is None:
                status.stale_since = now_dt
            elif new_connectivity == CONNECTIVITY_HEALTHY:
                status.stale_since = None
            status.updated_at = now_dt
            await add_event(
                db,
                event_type="CONNECTIVITY_CHANGED", actor_source="scheduler", agent_id=agent_id,
                summary=f"Connectivity {prev} -> {new_connectivity} after {int(delta.total_seconds())}s without contact",
            )
        await db.commit()

        # C4: RUNNING without a primary ACMS assignment = informational state.
        # Human direction 2026-09-29: do NOT automatically stop the runtime.
        # Emit once per state entry; auto-remediation is deliberately absent.
        await self._check_running_unassigned(db, status, now_dt)

        # periodic telemetry sample (plan §8)
        last = self._last_sample.get(agent_id)
        if last is None or (now_dt - last).total_seconds() >= s.telemetry_sample_seconds:
            await self._write_sample(db, status, now_dt)
            self._last_sample[agent_id] = now_dt

    async def _check_running_unassigned(self, db: AsyncSession, status: AgentStatusCurrentRecord,
                                         now_dt: datetime) -> None:
        """Informational warning when the harness reports the agent RUNNING but
        ACMS holds no ACTIVE primary assignment (Phase C4). No auto-remediation:
        the runtime keeps running; humans see the Attention item."""
        from sqlalchemy import select
        from .work_models import AssignmentRecord

        running = (status.agent_running or "").lower() == "true"
        in_state = self._running_unassigned.get(status.agent_id, False)
        if not running:
            if in_state:  # leaving the state: reset dedupe so a future entry re-emits
                self._running_unassigned[status.agent_id] = False
            return
        active = (await db.execute(
            select(AssignmentRecord).where(
                AssignmentRecord.agent_id == status.agent_id,
                AssignmentRecord.status == "ACTIVE",
            ).limit(1)
        )).scalars().first()
        if active is not None:
            if in_state:
                self._running_unassigned[status.agent_id] = False
            return
        if in_state:
            return  # already announced this entry — never per-tick spam
        self._running_unassigned[status.agent_id] = True
        await add_event(
            db,
            event_type="AGENT_RUNNING_WITHOUT_ASSIGNMENT",
            actor_source="scheduler", agent_id=status.agent_id,
            summary=("Agent RUNNING with no active primary ACMS assignment — "
                     "informational; no automatic remediation"),
            metadata={"detection": "scheduler", "policy": "observe-only"},
        )
        await db.commit()

    async def _attempt_reconciliation(self, db: AsyncSession, s, status: AgentStatusCurrentRecord, now_dt: datetime) -> bool:
        """Request a full status snapshot from the bridge (1 h stale path).

        Bridge transport lands in Phase 4; until then reconciliation of an
        agent without a live bridge fails (→ UNREACHABLE), which is the honest
        outcome: we cannot reach it."""
        await add_event(db, event_type="RECONCILIATION_STARTED",
            actor_source="scheduler", agent_id=status.agent_id,
            summary=f"Stale >= {s.stale_reconcile_seconds}s - requesting status snapshot")
        await db.commit()
        try:
            from .bridge import get_bridge_for_agent  # Phase 4 module

            bridge = get_bridge_for_agent(status.agent_id)
            snapshot = await bridge.fetch_status()
            await TelemetryService.ingest_heartbeat(db, status.agent_id, snapshot)
            await add_event(db, event_type="RECONCILIATION_SUCCEEDED",
                actor_source="reconciliation", agent_id=status.agent_id,
                summary="Reconciliation snapshot ingested; connectivity restored")
            await db.commit()
            return True
        except Exception as exc:  # noqa: BLE001 — no bridge / transport failure = unreachable
            await add_event(db, event_type="RECONCILIATION_FAILED",
                actor_source="reconciliation", agent_id=status.agent_id,
                summary=f"Reconciliation failed: {type(exc).__name__}",
                metadata={"error": str(exc)[:300]})
            await db.commit()
            return False

    async def _reconcile_execution_results(self, db: AsyncSession, s, now_dt: datetime) -> None:
        """ADR-0012 fallback: close nonterminal ACMS tasks/sessions whose A2A
        run is terminal on the bridge (lost callback / partition / restart /
        late callback repair path). Primary signal remains the callback.

        Rate-limited: sweeps at most every ``ACMS_CALLBACK_RECONCILE_SECONDS``
        (default 300s) so a tick loop never hammers the bridge.
        """
        interval = max(60, int(getattr(s, "callback_reconcile_seconds", 300)))
        last = self._last_result_reconcile
        if last is not None and (now_dt - last).total_seconds() < interval:
            return
        self._last_result_reconcile = now_dt

        from . import bridge as _bridge_mod

        get_bridge_for_agent = _bridge_mod.get_bridge_for_agent
        from .work_models import ExecutionTaskRecord

        nonterminal = (await db.execute(
            select(ExecutionTaskRecord)
            .where(ExecutionTaskRecord.status == "RUNNING")
            .where(ExecutionTaskRecord.external_task_id.isnot(None))
            .where(ExecutionTaskRecord.external_task_id.notlike("disp-%"))
            .order_by(ExecutionTaskRecord.started_at.asc())
            .limit(20)
        )).scalars().all()
        if not nonterminal:
            return
        repaired = 0
        for task in nonterminal:
            if not task.agent_id:
                continue  # unassigned task rows are never bridge-reconciled
            try:
                bridge = get_bridge_for_agent(task.agent_id)
                run = bridge._get(f"/v1/runs/{task.external_task_id}")
            except Exception:  # noqa: BLE001 — unreachable runtime: try next sweep
                continue
            run_status = (run or {}).get("status") or ""
            terminal_map = {"succeeded": "SUCCEEDED", "completed": "SUCCEEDED",
                            "failed": "FAILED", "cancelled": "CANCELLED",
                            "canceled": "CANCELLED"}
            mapped = terminal_map.get(run_status.lower())
            if not mapped:
                continue
            task.status = mapped
            task.finished_at = now_dt
            # ADR-0012 §C5: reconciliation repairs SESSION state too — close the
            # OPEN ExecutionSession correlated to this A2A run (fail-safe: never
            # reopen or fabricate; only OPEN sessions for the same run id).
            from .memory_models import ExecutionSessionRecord as _ESR

            sess = (await db.scalars(
                select(_ESR)
                .where(_ESR.a2a_task_id == task.external_task_id)
                .where(_ESR.status == "OPEN")
                .order_by(_ESR.started_at.desc())
                .limit(1)
            )).first()
            session_id = None
            if sess is not None:
                sess.status = "CLOSED"
                sess.ended_at = now_dt
                session_id = sess.session_id
            repaired += 1
            await add_event(
                db, event_type="EXECUTION_RECONCILED", actor_source="result_reconciliation",
                agent_id=task.agent_id, work_key=None,
                a2a_task_id=task.external_task_id,
                summary=(f"Reconciler closed task {task.task_id[:12]} → {mapped} "
                         f"from bridge run status '{run_status}' (callback lost/late)"),
                metadata={"task_id": task.task_id, "a2a_run_id": task.external_task_id,
                          "session_id": session_id, "source": "adr0012_reconciliation"},
            )
        if repaired:
            await db.commit()

    async def _maybe_fleet_reconcile(self, db: AsyncSession, s, now_dt: datetime) -> None:
        if self._last_fleet_reconcile is None:
            self._last_fleet_reconcile = now_dt
            return
        if (now_dt - self._last_fleet_reconcile).total_seconds() < s.fleet_reconcile_seconds:
            return
        self._last_fleet_reconcile = now_dt
        logger.info("fleet reconciliation tick at %s", now_dt.isoformat())
        # Phase 4: per-agent reconciliation attempts for agents with bridges.
        # Management scope is never touched here (ADR-0010).

    async def _write_sample(self, db: AsyncSession, status: AgentStatusCurrentRecord, now_dt: datetime) -> None:
        db.add(AgentTelemetrySampleRecord(
            sample_id=_new_id(), agent_id=status.agent_id, sampled_at=now_dt,
            agent_running=status.agent_running, connectivity=status.connectivity,
            model_id=status.model_id,
            context_used_tokens=status.context_used_tokens,
            context_max_tokens=status.context_max_tokens,
            context_utilization_percent=status.context_utilization_percent,
            context_warning=status.context_warning,
            session_id=status.session_id, session_title=status.session_title,
            session_total_tokens=status.session_total_tokens,
            cumulative_api_tokens=status.cumulative_api_tokens,
        ))
        await db.commit()
