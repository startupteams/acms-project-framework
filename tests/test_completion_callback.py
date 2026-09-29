"""ADR-0012 worker completion callback tests (Accepted 2026-09-29).

Covers: happy path (task terminal + session CLOSED, no manual close),
idempotent duplicate/late callback, unknown task id (404 + audit),
agent mismatch (403 fail-closed + audit), invalid status (422),
endpoint auth (unset token 503 fail-closed / missing 401 / wrong 403),
and the reconciliation fallback sweep (bridge-terminal run closes the task).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from acms.memory_models import ExecutionSessionRecord
from acms.telemetry_models import AgentEventRecord
from acms.telemetry_service import _now
from acms.work_models import ExecutionTaskRecord


@pytest.fixture()
async def db(clean_db):
    from acms.db import get_session

    async for session in get_session():
        yield session


@pytest.fixture(autouse=True)
def _callback_token(monkeypatch):
    monkeypatch.setenv("ACMS_CALLBACK_TOKEN", "cb-secret")
    from acms.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _mk_task(db, *, agent_id="agent-A", external="run-123", status="RUNNING"):
    rec = ExecutionTaskRecord(
        task_id=str(uuid.uuid4()), work_item_id="w-1", agent_id=agent_id,
        external_task_id=external, status=status, started_at=_now(),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def _mk_open_session(db, *, agent_id="agent-A", a2a="run-123"):
    rec = ExecutionSessionRecord(
        session_id=ExecutionSessionRecord.new_id(), agent_id=agent_id,
        work_item_id="w-1", a2a_task_id=a2a, status="OPEN", started_at=_now(),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def _events(db, event_type):
    return (await db.execute(
        select(AgentEventRecord).where(AgentEventRecord.event_type == event_type)
    )).scalars().all()


async def test_callback_success_closes_task_and_session(db):
    from acms.completion_callback import CompletionCallbackRequest, process_completion_callback

    task = await _mk_task(db)
    session = await _mk_open_session(db)

    out = await process_completion_callback(
        db, CompletionCallbackRequest(task_id=task.task_id, acms_agent_id="agent-A",
                                      a2a_run_id="run-123", status="SUCCEEDED"))
    assert out["result"] == "closed"

    await db.refresh(task)
    await db.refresh(session)
    assert task.status == "SUCCEEDED"
    assert task.finished_at is not None
    assert session.status == "CLOSED"
    assert session.ended_at is not None
    assert len(await _events(db, "EXECUTION_COMPLETED")) == 1


async def test_duplicate_and_late_callbacks_are_idempotent(db):
    from acms.completion_callback import CompletionCallbackRequest, process_completion_callback

    task = await _mk_task(db)
    await _mk_open_session(db)

    def cb(status):
        return CompletionCallbackRequest(task_id=task.task_id, acms_agent_id="agent-A",
                                         a2a_run_id="run-123", status=status)

    assert (await process_completion_callback(db, cb("SUCCEEDED")))["result"] == "closed"
    assert (await process_completion_callback(db, cb("SUCCEEDED")))["result"] == "already_terminal"
    # a late conflicting status must NOT rewrite history
    assert (await process_completion_callback(db, cb("FAILED")))["result"] == "already_terminal"
    await db.refresh(task)
    assert task.status == "SUCCEEDED"


async def test_unknown_task_id_rejected_with_audit(db):
    from acms.completion_callback import (
        CompletionCallbackError,
        CompletionCallbackRequest,
        process_completion_callback,
    )

    with pytest.raises(CompletionCallbackError) as ei:
        await process_completion_callback(
            db, CompletionCallbackRequest(task_id="no-such-task", acms_agent_id="agent-A",
                                          a2a_run_id="run-x", status="SUCCEEDED"))
    assert ei.value.code == "unknown-task-id" and ei.value.http_status == 404
    assert len(await _events(db, "EXECUTION_CALLBACK_REJECTED")) == 1


async def test_agent_mismatch_fails_closed_with_audit(db):
    from acms.completion_callback import (
        CompletionCallbackError,
        CompletionCallbackRequest,
        process_completion_callback,
    )

    task = await _mk_task(db, agent_id="agent-A")
    with pytest.raises(CompletionCallbackError) as ei:
        await process_completion_callback(
            db, CompletionCallbackRequest(task_id=task.task_id, acms_agent_id="agent-B",
                                          a2a_run_id="run-123", status="SUCCEEDED"))
    assert ei.value.code == "agent-mismatch" and ei.value.http_status == 403
    assert len(await _events(db, "EXECUTION_CALLBACK_REJECTED")) == 1
    await db.refresh(task)
    assert task.status == "RUNNING"  # untouched


async def test_invalid_status_rejected(db):
    from acms.completion_callback import (
        CompletionCallbackError,
        CompletionCallbackRequest,
        process_completion_callback,
    )

    task = await _mk_task(db)
    with pytest.raises(CompletionCallbackError) as ei:
        await process_completion_callback(
            db, CompletionCallbackRequest(task_id=task.task_id, acms_agent_id="agent-A",
                                          a2a_run_id="run-123", status="CLOSED"))
    assert ei.value.code == "invalid-status"


# ------------------------------------------------------------------ endpoint


@pytest.fixture()
def client(db, monkeypatch):
    from acms.db import get_session
    from acms.main import app
    from acms.settings import get_settings

    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    # get_settings() is lru_cached; the env fixture already set the token —
    # patch the cached instance so the dependency sees it too.
    monkeypatch.setattr(get_settings(), "callback_token", "cb-secret")
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def test_endpoint_auth_and_happy_path(client, db):
    r = client.post("/api/v1/callbacks/execution-completion",
                    json={"task_id": "x", "acms_agent_id": "a", "status": "SUCCEEDED"})
    assert r.status_code == 401  # missing token

    r = client.post("/api/v1/callbacks/execution-completion",
                    json={"task_id": "x", "acms_agent_id": "a", "status": "SUCCEEDED"},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403  # wrong token

    task = _run(_mk_task(db))
    session = _run(_mk_open_session(db))
    r = client.post("/api/v1/callbacks/execution-completion",
                    json={"task_id": task.task_id, "acms_agent_id": "agent-A",
                          "a2a_run_id": "run-123", "status": "SUCCEEDED",
                          "result_reference": "pr:123", "usage_reference": {"tokens": 84}},
                    headers={"Authorization": "Bearer cb-secret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "closed" and body["task_id"] == task.task_id
    _run(db.refresh(task))
    _run(db.refresh(session))
    assert task.status == "SUCCEEDED" and session.status == "CLOSED"


def test_endpoint_unset_token_fails_closed(monkeypatch):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setenv("ACMS_CALLBACK_TOKEN", "")
    get_settings.cache_clear()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/v1/callbacks/execution-completion",
                        json={"task_id": "x", "acms_agent_id": "a",
                              "status": "SUCCEEDED"},
                        headers={"Authorization": "Bearer cb-secret"})
        assert r.status_code == 503
    finally:
        get_settings.cache_clear()


def _run(awaitable):
    """Run a coroutine inside a sync test via asyncio (single event loop)."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(awaitable)


# ------------------------------------------------------- reconciliation sweep


class _FakeBridge:
    def __init__(self, target):
        pass

    def _get(self, path):
        assert path.endswith("/v1/runs/run-recon-1")
        return {"status": "succeeded"}


class _S:
    callback_reconcile_seconds = 60
    fleet_reconcile_seconds = 999999
    heartbeat_stale_seconds = 300
    stale_reconcile_seconds = 3600
    telemetry_sample_seconds = 300


async def test_reconciler_closes_task_from_bridge_terminal_status(db, monkeypatch):
    from acms.telemetry_scheduler import TelemetryScheduler
    import acms.bridge as bridge_mod

    task = await _mk_task(db, external="run-recon-1")
    await _mk_open_session(db, a2a="run-recon-1")

    monkeypatch.setattr(bridge_mod, "get_bridge_for_agent", lambda aid: _FakeBridge(None))

    sched = TelemetryScheduler(session_factory=None)
    sched._last_result_reconcile = None
    await sched._reconcile_execution_results(db, _S(), datetime.now(timezone.utc))

    await db.refresh(task)
    assert task.status == "SUCCEEDED"
    assert len(await _events(db, "EXECUTION_RECONCILED")) == 1

    # rate limit: an immediate second sweep inside the interval is a no-op
    now2 = datetime.now(timezone.utc)
    sched._last_result_reconcile = now2
    await sched._reconcile_execution_results(db, _S(), now2 + timedelta(seconds=10))
    await db.refresh(task)
    assert task.status == "SUCCEEDED"
    assert len(await _events(db, "EXECUTION_RECONCILED")) == 1


async def test_reconciler_ignores_disp_prefixed_and_open_runs(db, monkeypatch):
    from acms.telemetry_scheduler import TelemetryScheduler
    import acms.bridge as bridge_mod

    # dedupe-key task (disp-*) is NOT a live A2A run id — must never be swept
    task = await _mk_task(db, external="disp-abc123")
    monkeypatch.setattr(bridge_mod, "get_bridge_for_agent", lambda aid: _FakeBridge(None))

    sched = TelemetryScheduler(session_factory=None)
    sched._last_result_reconcile = None
    await sched._reconcile_execution_results(db, _S(), datetime.now(timezone.utc))

    await db.refresh(task)
    assert task.status == "RUNNING"
    assert len(await _events(db, "EXECUTION_RECONCILED")) == 0