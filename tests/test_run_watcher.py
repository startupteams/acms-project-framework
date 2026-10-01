"""RunWatcher tests — ADR-0012 runtime-driven callback (window-4 Phase C).

Covers plan §C3 (retry policy), §C4 (idempotency semantics), and the dispatch
wiring. All time is injected; no real network, no real sleeps.
"""
from __future__ import annotations

import asyncio

import pytest

from acms.run_watcher import WatchDeps, testing_registry


class FakeBridge:
    def __init__(self, statuses: list[str | dict | Exception]):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, agent_id: str, run_id: str) -> dict:
        self.calls += 1
        s = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        if isinstance(s, Exception):
            raise s
        return s if isinstance(s, dict) else {"status": s}


class FakePoster:
    """Records callback deliveries; scripted results per attempt."""

    def __init__(self, results: list[tuple[bool, bool]] | None = None):
        self.results = list(results or [])
        self.payloads: list[dict] = []
        self.all_ok = results is None

    def __call__(self, payload: dict) -> tuple[bool, bool]:
        self.payloads.append(payload)
        if self.all_ok:
            return True, False
        return self.results.pop(0) if self.results else (True, False)



async def _mk_task(db, *, status="RUNNING", run_id="run_x") -> tuple[str, str]:
    """One WorkItem + ExecutionTask row (work_item_id is NOT NULL). Returns ids."""
    from datetime import datetime, timezone

    from acms.work_models import ExecutionTaskRecord, WorkItemRecord
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    work_id = str(uuid4())
    db.add(WorkItemRecord(work_item_id=work_id, title="watcher test work",
                           created_at=now, updated_at=now))
    task_id = str(uuid4())
    db.add(ExecutionTaskRecord(task_id=task_id, work_item_id=work_id,
                               agent_id=str(uuid4()),
                               external_task_id=run_id, status=status,
                               started_at=now))
    await db.commit()
    return task_id, work_id

async def _mk_task_with_agent(db, *, run_id="run_abc") -> tuple[str, str]:
    """WorkItem + ExecutionTask; returns (task_id, agent_id) for identity asserts."""
    from acms.work_models import ExecutionTaskRecord, WorkItemRecord
    from uuid import uuid4

    from datetime import datetime, timezone

    from acms.work_models import ExecutionTaskRecord, WorkItemRecord
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    work_id = str(uuid4())
    db.add(WorkItemRecord(work_item_id=work_id, title="watcher test work",
                           created_at=now, updated_at=now))
    task_id = str(uuid4())
    agent_id = str(uuid4())
    db.add(ExecutionTaskRecord(task_id=task_id, work_item_id=work_id,
                               agent_id=agent_id,
                               external_task_id=run_id, status="RUNNING",
                               started_at=now))
    await db.commit()
    return task_id, agent_id


def make_deps(bridge, poster, poll=0.01, retries=(0.0, 0.01, 0.01, 0.01), max_s=5.0):
    return WatchDeps(bridge_get=bridge, post_callback=poster,
                     poll_seconds=poll, retry_delays=retries, max_seconds=max_s)


@pytest.fixture()
async def db(clean_db):
    from acms.db import get_session

    async for session in get_session():
        yield session


@pytest.fixture()
def _unused_task_fixture():
    """Placeholder to keep fixture ordering stable; unused."""
    return None


@pytest.mark.asyncio
async def test_watcher_delivers_callback_on_terminal(db):
    """Terminal bridge run → one authenticated callback delivery."""

    task_id, agent_id = await _mk_task_with_agent(db, run_id="run_abc")

    poster = FakePoster()
    reg = testing_registry()
    reg._watches[task_id] = asyncio.create_task(
        reg._watch(task_id, make_deps(FakeBridge(["succeeded"]), poster)))

    # wait for the watcher to finish its single cycle
    for _ in range(200):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break
    assert poster.payloads, "watcher never delivered the callback"
    p = poster.payloads[0]
    assert p["task_id"] == task_id
    assert p["acms_agent_id"] == agent_id
    assert p["a2a_run_id"] == "run_abc"
    assert p["status"] == "SUCCEEDED"
    assert "completed_at" in p


@pytest.mark.asyncio
async def test_watcher_retries_transient_then_succeeds(db):
    """§C3: delivery failure (network/5xx) retries with backoff, then succeeds."""

    task_id, _ = await _mk_task(db, run_id="run_r", status="RUNNING")

    poster = FakePoster(results=[(False, False), (False, False), (True, False)])
    reg = testing_registry()
    reg._watches[task_id] = asyncio.create_task(
        reg._watch(task_id, make_deps(FakeBridge(["succeeded"]), poster)))
    for _ in range(400):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break
    assert len(poster.payloads) == 3, f"expected 3 delivery attempts, got {len(poster.payloads)}"


@pytest.mark.asyncio
async def test_watcher_stops_on_permanent_rejection(db):
    """403/404/503-class failures stop the watch immediately (no retry)."""

    task_id, _ = await _mk_task(db, run_id="run_p", status="RUNNING")

    poster = FakePoster(results=[(False, True)])
    reg = testing_registry()
    reg._watches[task_id] = asyncio.create_task(
        reg._watch(task_id, make_deps(FakeBridge(["succeeded"]), poster)))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break
    assert len(poster.payloads) == 1, "permanent rejection must not retry"


@pytest.mark.asyncio
async def test_watcher_noop_when_already_terminal(db):
    """§C4: task closed by a worker callback/reconcile → watcher exits with
    zero deliveries (idempotent no-op, no duplicate close)."""

    task_id, _ = await _mk_task(db, run_id="run_t", status="SUCCEEDED")

    poster = FakePoster()
    reg = testing_registry()
    reg._watches[task_id] = asyncio.create_task(
        reg._watch(task_id, make_deps(FakeBridge(["succeeded"]), poster)))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break
    assert poster.payloads == [], "already-terminal task must not be re-closed"


@pytest.mark.asyncio
async def test_watcher_bridge_unreachable_polls_until_deadline(db):
    """Bridge down → watcher keeps polling (no delivery, no crash) then expires."""
    from acms.bridge import BridgeError

    task_id, _ = await _mk_task(db, run_id="run_u", status="RUNNING")

    poster = FakePoster()
    bridge = FakeBridge([BridgeError("down"), BridgeError("down"), BridgeError("down")])
    reg = testing_registry()
    reg._watches[task_id] = asyncio.create_task(
        reg._watch(task_id, make_deps(bridge, poster, poll=0.01, max_s=0.05)))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break
    assert poster.payloads == []
    assert bridge.calls >= 1


@pytest.mark.asyncio
async def test_watcher_failed_run_carries_error_summary(db):

    task_id, _ = await _mk_task(db, run_id="run_f", status="RUNNING")

    poster = FakePoster()
    reg = testing_registry()
    reg._watches[task_id] = asyncio.create_task(reg._watch(
        task_id, make_deps(FakeBridge([{"status": "failed", "error": "boom: bad input"}]), poster)))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break
    assert poster.payloads and poster.payloads[0]["status"] == "FAILED"
    assert "boom" in (poster.payloads[0].get("error_summary") or "")


@pytest.mark.asyncio
async def test_watcher_carries_run_usage_as_usage_reference(db):
    """Live-found 2026-10-01: the watcher callback carried no usage, so the
    cost_attribution row stored NULL tokens even though the bridge run
    reported real usage. The callback must pass run.usage through."""
    from uuid import uuid4

    from datetime import datetime, timezone

    from acms.work_models import ExecutionTaskRecord, WorkItemRecord

    now = datetime.now(timezone.utc)
    work_id = str(uuid4())
    db.add(WorkItemRecord(work_item_id=work_id, title="usage watcher test",
                          created_at=now, updated_at=now))
    task_id = str(uuid4())
    db.add(ExecutionTaskRecord(task_id=task_id, work_item_id=work_id,
                               agent_id=str(uuid4()),
                               external_task_id="run_u", status="RUNNING",
                               started_at=now))
    await db.commit()

    poster = FakePoster()
    reg = testing_registry()
    run = {"status": "completed",
           "usage": {"input_tokens": 13605, "output_tokens": 84,
                     "total_tokens": 13689}}
    reg._watches[task_id] = asyncio.create_task(reg._watch(
        task_id, make_deps(FakeBridge([run]), poster)))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break

    assert poster.payloads, "watcher delivered nothing"
    p = poster.payloads[0]
    assert p["status"] == "SUCCEEDED"
    assert p["usage_reference"] == {"input_tokens": 13605, "output_tokens": 84,
                                    "total_tokens": 13689}


@pytest.mark.asyncio
async def test_watcher_omits_malformed_usage(db):
    """Non-int / negative usage values must be filtered, not forwarded."""
    from uuid import uuid4

    from datetime import datetime, timezone

    from acms.work_models import ExecutionTaskRecord, WorkItemRecord

    now = datetime.now(timezone.utc)
    work_id = str(uuid4())
    db.add(WorkItemRecord(work_item_id=work_id, title="bad usage watcher test",
                          created_at=now, updated_at=now))
    task_id = str(uuid4())
    db.add(ExecutionTaskRecord(task_id=task_id, work_item_id=work_id,
                               agent_id=str(uuid4()),
                               external_task_id="run_u2", status="RUNNING",
                               started_at=now))
    await db.commit()

    poster = FakePoster()
    reg = testing_registry()
    run = {"status": "completed",
           "usage": {"input_tokens": "lots", "output_tokens": -5}}
    reg._watches[task_id] = asyncio.create_task(reg._watch(
        task_id, make_deps(FakeBridge([run]), poster)))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if task_id not in reg._watches:
            break

    assert poster.payloads and "usage_reference" not in poster.payloads[0]
