"""Run-event pump (ADR-0014; STEA-004 plan §15).

For a dispatched run, ACMS subscribes to the worker's Hermes api-server SSE
stream (`GET /v1/runs/{run_id}/events`) in the background, sanitizes events,
and persists them to `execution_events` (one row per seq, task-scoped). The
browser never talks to worker VMs; it consumes ACMS-side
`/api/v1/execution/{task_id}/stream` which replays the persisted rows.

Pump lifecycle:
- scheduled once per (task_id) — idempotent; stop on terminal run status
- bounded lifetime (RUN_WATCH_MAX_SECONDS class) so dead runs can't pin tasks
- fail-open: pump errors never break the run or the completion callback
- the run watcher (completion) stays the terminal-state authority; this pump
  only provides live visibility.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from sqlalchemy import select

from .db import SessionLocal
from .a2a_models import ExecutionEventRecord
from .work_models import ExecutionTaskRecord

logger = logging.getLogger("acms.a2a.run_pump")

_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_MAX_TASKS_WATCHED = 32


class RunEventPumpRegistry:
    """In-process registry: one pump task per task_id (idempotent scheduling)."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def schedule(self, task_id: str) -> None:
        if task_id in self._tasks and not self._tasks[task_id].done():
            return  # already pumping
        if len([t for t in self._tasks.values() if not t.done()]) >= _MAX_TASKS_WATCHED:
            logger.warning("run-event pump registry saturated; skipping task %s", task_id)
            return
        self._tasks[task_id] = asyncio.create_task(
            _pump_events(task_id), name=f"acms-run-pump-{task_id[:8]}")

    def stop_all(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()


_pumps = RunEventPumpRegistry()


def schedule_run_event_pump(task_id: str) -> None:
    """Fail-open scheduling (call after commit; errors never break dispatch).

    Disabled when ``ACMS_RUN_EVENT_PUMP_ENABLED=false`` (unit tests use it:
    a pump polling the shared SQLite DB in a different event loop causes
    'database is locked' cascades)."""
    try:
        from .settings import get_settings

        if not getattr(get_settings(), "run_event_pump_enabled", True):
            return
        _pumps.schedule(task_id)
    except Exception:  # noqa: BLE001 — fail-open by design
        logger.exception("run-event pump scheduling failed for %s", task_id)


def _bridge_target_for_task(task: ExecutionTaskRecord):
    """Resolve the bridge, verifying it is a real Hermes bridge (has _t with
    base_url) that can serve run-events. Fakes / unsupported bridges → None
    (pump exits after one bounded retry rather than polling forever)."""
    from .bridge import HermesBridge, get_bridge_for_agent

    if task.agent_id is None:
        return None
    try:
        bridge = get_bridge_for_agent(task.agent_id)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(bridge, HermesBridge) and getattr(bridge, "_t", None):
        return bridge
    return None


def _sse_events(resp) -> "list[dict] | None":
    """Read the SSE stream until terminal event/keepalive timeout; return parsed
    event dicts. Blocks (executor-run); bounded by resp timeout."""
    events: list[dict] = []
    buf = b""
    import time

    start = time.monotonic()
    while time.monotonic() - start < 25:  # bounded read window per poll
        chunk = resp.read1(8192)
        if not chunk:
            break
        buf += chunk
        lines = buf.split(b"\n\n")
        buf = lines.pop()  # tail
        for block in lines:
            for line in block.split(b"\n"):
                if line.startswith(b"data: "):
                    try:
                        events.append(json.loads(line[6:].decode()))
                    except (ValueError, UnicodeDecodeError):
                        continue
        if any(e.get("event", "").startswith("run.") for e in events):
            break
    return events or None


def _sanitize(ev: dict) -> dict:
    """Same allow-list as the ingestion API (ADR-0014 §14: no secrets/reasoning)."""
    out = {}
    for key in ("event", "run_id", "tool", "duration", "error"):
        if ev.get(key) is not None:
            out[key] = ev[key]
    for key, cap in (("preview", 500), ("delta", 500), ("output", 2000)):
        if ev.get(key) is not None:
            out[key] = str(ev[key])[:cap]
    if isinstance(ev.get("usage"), dict):
        out["usage"] = {k: ev["usage"].get(k) for k in ("input_tokens", "output_tokens", "total_tokens")
                        if ev["usage"].get(k) is not None}
    return out


async def _pump_events(task_id: str) -> None:
    """Poll the run status; while the run is live, pull the SSE stream window
    and persist any new events. Poll-based (not persistent-stream) so ACMS
    restarts resume cleanly and the pump never holds a socket for hours."""
    from .settings import get_settings

    poll_s = 5
    unresolvable_rounds = 0
    import time as _time

    started = _time.monotonic()
    max_seconds = 7200  # hard lifetime cap (matches run-watch bound)
    try:
        from .settings import get_settings as _gs
        max_seconds = _gs().callback_watch_max_seconds
    except Exception:  # noqa: BLE001
        pass
    while True:
        if _time.monotonic() - started > max_seconds:
            logger.info("run pump lifetime cap reached for task %s", task_id)
            return
        async with SessionLocal() as db:
            task = await db.get(ExecutionTaskRecord, task_id)
            if task is None or task.status != "RUNNING" or not task.external_task_id:
                return
            bridge = _bridge_target_for_task(task)
            run_id = task.external_task_id
        if bridge is None:
            # unresolvable/unsupported bridge: exit immediately (fail-open —
            # the completion watcher + reconcile remain the terminal authorities)
            return
        # blocking HTTP in executor to keep the loop healthy
        loop = asyncio.get_running_loop()

        def _fetch():
            if bridge is None or bridge._t is None:
                return None
            url = f"{bridge._t.base_url}/v1/runs/{run_id}/events"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {bridge._t.api_key}")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return _sse_events(resp)
            except Exception as e:  # noqa: BLE001
                logger.debug("run pump fetch %s: %s", run_id[:16], e)
                return None

        events = await loop.run_in_executor(None, _fetch)
        if events:
            async with SessionLocal() as db:
                max_seq = (await db.execute(
                    select(ExecutionEventRecord.seq)
                    .where(ExecutionEventRecord.task_id == task_id)
                    .order_by(ExecutionEventRecord.seq.desc()).limit(1))).scalar()
                seq = max_seq or 0
                new = 0
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    seq += 1
                    etype = str(ev.get("event") or "unknown")[:64]
                    db.add(ExecutionEventRecord(
                        task_id=task_id, seq=seq, event_type=etype,
                        occurred_at=now,
                        payload_json=json.dumps(_sanitize(ev))[:8000]))
                    new += 1
                if new:
                    await db.commit()
                terminal = any(
                    isinstance(e, dict) and e.get("event") in
                    ("run.completed", "run.failed", "run.cancelled")
                    for e in events if isinstance(e, dict))
                if terminal:
                    return
        else:
            # no events visible: stop when the run is already terminal (cheap
            # status check) — the SSE endpoint 404s after run TTL anyway.
            async with SessionLocal() as db:
                t2 = await db.get(ExecutionTaskRecord, task_id)
                if t2 is None or t2.status != "RUNNING":
                    return
        await asyncio.sleep(poll_s)