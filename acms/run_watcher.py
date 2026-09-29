"""ADR-0012 runtime-driven completion callback (window-4 plan §5 / Phase C).

Problem this solves (live finding, window 3): the in-instruction COMPLETION
PROTOCOL depends on the model choosing to comply — the GLM-fast worker
ignored it, and reconciliation became the de-facto completion authority.
Per plan §C1 the callback must be **infrastructure behavior, not model
behavior**: the bridge/runtime layer observes the A2A run's authoritative
terminal state and performs the callback automatically.

Mechanism:
    At dispatch time ACMS schedules a bounded in-process watcher (one asyncio
    task per execution task, same durability class as TelemetryScheduler —
    no Celery/event bus). The watcher polls the bridge run status; on a
    terminal state (SUCCEEDED/FAILED/CANCELLED) it delivers the EXACT
    ADR-0012 callback — authenticated HTTP POST to the same
    /api/v1/callbacks/execution-completion endpoint a worker runtime would
    use — so the full callback path (token auth, identity binding,
    idempotency, audit) is exercised, not bypassed.

    The model-facing COMPLETION PROTOCOL hint in the dispatch instruction
    remains as an additional optional path; duplicate delivery is harmless
    (the endpoint is idempotent). The reconcile sweep remains the final
    fallback for lost callbacks, ACMS restarts, and watch expiry.

Retry policy (plan §C3): immediate → 10s → 30s → 60s on transient delivery
failure (network/5xx), then stop — reconciliation takes over. Permanent
rejections (403 identity, 404 unknown task, 503 fail-closed config) stop
the watch immediately; retrying cannot fix a contract/config error.

Poll cadence and lifetime are bounded: ACMS_CALLBACK_WATCH_POLL_SECONDS
(default 30) and ACMS_CALLBACK_WATCH_MAX_SECONDS (default 7200). After
expiry the watch logs RUN_WATCH_EXPIRED and stops — the reconciler sweep
(every ACMS_CALLBACK_RECONCILE_SECONDS) stays the completion authority.

Multi-replica note: each ACMS replica would schedule its own watcher; the
callback endpoint is idempotent, so duplicate deliveries collapse to one
terminal transition (documented; single-replica per TDR-0009 today).
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .bridge import BridgeError
from .telemetry_service import add_event

logger = logging.getLogger("acms.run_watcher")

# Bridge run status → ADR-0012 callback status (same mapping the reconciler
# uses — one vocabulary everywhere).
RUN_STATUS_MAP = {
    "succeeded": "SUCCEEDED",
    "completed": "SUCCEEDED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
}

# Delivery failure classes: transient → retry per plan §C3; permanent → stop.
_PERMANENT_HTTP = {403, 404, 503}  # identity/unknown-task/fail-closed-config


@dataclass
class WatchDeps:
    """Injected production/test dependencies (TelemetryScheduler pattern)."""

    # bridge_get(agent_id, run_id) → run dict; raises BridgeError on transport failure
    bridge_get: Callable[[str, str], Any]
    # post_callback(payload) → (delivered: bool, permanent: bool)
    post_callback: Callable[[dict], tuple[bool, bool]]
    poll_seconds: float = 30.0
    retry_delays: tuple[float, ...] = (0.0, 10.0, 30.0, 60.0)
    max_seconds: float = 7200.0


class RunWatchRegistry:
    """task_id → watcher asyncio.Task. Idempotent per task_id."""

    def __init__(self) -> None:
        self._watches: dict[str, asyncio.Task] = {}

    def schedule(self, task_id: str, deps: WatchDeps) -> bool:
        if task_id in self._watches and not self._watches[task_id].done():
            return False  # duplicate dispatch never double-watches
        self._watches[task_id] = asyncio.create_task(
            self._watch(task_id, deps), name=f"acms-run-watch-{task_id[:12]}")
        return True

    def get(self, task_id: str) -> asyncio.Task | None:
        return self._watches.get(task_id)

    async def _watch(self, task_id: str, deps: WatchDeps) -> None:
        from .db import SessionLocal
        from .memory_models import ExecutionSessionRecord
        from .work_models import ExecutionTaskRecord
        from sqlalchemy import select

        deadline = datetime.now(timezone.utc).timestamp() + deps.max_seconds
        try:
            while datetime.now(timezone.utc).timestamp() < deadline:
                await asyncio.sleep(deps.poll_seconds)

                # --- observe current ACMS + bridge state (fresh session each
                # poll — the watcher never holds a request-scoped session) ---
                async with SessionLocal() as db:
                    task = await db.get(ExecutionTaskRecord, task_id)
                    if task is None:
                        return  # dispatch aborted; nothing to watch
                    if task.status != "RUNNING":
                        return  # closed by a worker callback / reconcile — done
                    run_id = task.external_task_id or ""
                    if (not run_id) or run_id.startswith("disp-"):
                        return  # never actually dispatched; nothing to watch
                    agent_id = task.agent_id or ""

                try:
                    run = deps.bridge_get(agent_id, run_id)
                except BridgeError:
                    continue  # transient bridge failure: keep polling to deadline
                status = RUN_STATUS_MAP.get(((run or {}).get("status") or "").lower())
                if not status:
                    continue  # still running / unknown status: keep polling

                # --- deliver the ADR-0012 callback with bounded backoff -----
                payload = {
                    "task_id": task_id,
                    "acms_agent_id": agent_id,
                    "a2a_run_id": run_id,
                    "status": status,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                if status == "FAILED":
                    err = (run or {}).get("error") or (run or {}).get("detail")
                    if err:
                        payload["error_summary"] = str(err)[:512]

                delivered = False
                for delay in deps.retry_delays:
                    if delay:
                        await asyncio.sleep(delay)
                    ok, permanent = deps.post_callback(payload)
                    if ok:
                        delivered = True
                        break
                    if permanent:
                        await self._audit(db_factory=SessionLocal, event="RUN_WATCH_STOPPED",
                                          task_id=task_id, run_id=run_id, detail="permanent rejection")
                        return
                if delivered:
                    return  # callback accepted; reconcile has nothing to repair

                await self._audit(db_factory=SessionLocal, event="RUN_WATCH_RETRIES_EXHAUSTED",
                                  task_id=task_id, run_id=run_id,
                                  detail="reconciliation remains the completion authority")
                return

            # deadline passed with no terminal observation
            await self._audit(db_factory=SessionLocal, event="RUN_WATCH_EXPIRED",
                              task_id=task_id, run_id="",
                              detail=f"watch lifetime {deps.max_seconds}s elapsed; reconcile covers")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a watcher must never crash the app
            logger.exception("run watcher failed for task %s", task_id[:12])
        finally:
            self._watches.pop(task_id, None)

    async def _audit(self, *, db_factory, event: str, task_id: str,
                     run_id: str, detail: str) -> None:
        async with db_factory() as db:
            await add_event(
                db, event_type=event, actor_source="runtime_watch",
                summary=f"{event}: task {task_id[:12]} — {detail}"[:512],
                metadata={"task_id": task_id, "a2a_run_id": run_id,
                          "source": "adr0012_runtime_watch"},
            )
            await db.commit()


# Module-level registry (single ACMS instance per TDR-0009).
_registry = RunWatchRegistry()


def _production_deps() -> WatchDeps:
    """Wire the watcher to the real bridge + the real authenticated callback
    endpoint (in-container loopback: nginx does not allowlist compose-network
    sources, but the app serves :8000 inside its own container)."""
    from .settings import get_settings

    s = get_settings()

    def bridge_get(agent_id: str, run_id: str) -> dict:
        from .bridge import get_bridge_for_agent

        bridge = get_bridge_for_agent(agent_id)
        return bridge._get(f"/v1/runs/{run_id}")

    def post_callback(payload: dict) -> tuple[bool, bool]:
        url = f"{(s.callback_self_url or 'http://127.0.0.1:8000').rstrip('/')}" \
              f"/api/v1/callbacks/execution-completion"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {s.callback_token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status == 200, False
        except urllib.error.HTTPError as e:
            return False, e.code in _PERMANENT_HTTP
        except (urllib.error.URLError, TimeoutError, OSError):
            return False, False

    return WatchDeps(
        bridge_get=bridge_get,
        post_callback=post_callback,
        poll_seconds=max(5, s.callback_watch_poll_seconds),
        retry_delays=(0.0, 10.0, 30.0, 60.0),
        max_seconds=max(60, s.callback_watch_max_seconds),
    )


def schedule_run_watch(task_id: str, *, enabled: bool | None = None) -> bool:
    """Schedule the runtime-driven callback watcher for one execution task.

    Called by dispatch_service after a successful bridge dispatch. Never
    raises — watch scheduling must not break dispatch (failures log only).
    Returns True when a watcher was scheduled, False when disabled/duplicate.
    """
    from .settings import get_settings

    try:
        s = get_settings()
        if enabled is None:
            enabled = s.callback_watch_enabled and bool(s.callback_token) and bool(s.callback_base_url)
        if not enabled:
            return False
        return _registry.schedule(task_id, _production_deps())
    except Exception:  # noqa: BLE001
        logger.exception("scheduling run watcher failed for task %s", task_id[:12])
        return False


def testing_registry() -> RunWatchRegistry:
    """Fresh registry for tests (never touches the module singleton)."""
    return RunWatchRegistry()
