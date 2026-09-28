"""SSE transport for ACMS semantic events (REV2 Phase D — plan §6-D2).

The durable audit log (agent_events) is the ONLY source; SSE is a transport,
never a second store. Browser subscribes to ACMS once (not to every agent).

Contract:
- text/event-stream; ordered by the durable event sequence
- ``Last-Event-ID`` (or ?last_id=) resumes AFTER that id — safe replay window,
  no duplicate semantic delivery after reconnect
- semantic events only (allow-listed types); token telemetry never qualifies
- auth: the machine bearer token (same as /api/v1/*); tenant/trust boundaries
  preserved (this is the internal control-plane stream, admin-scoped)
- heartbeat comments every ~15s keep proxies from closing idle connections
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .db import get_session
from .security import require_admin_token
from .telemetry_models import AgentEventRecord

router = APIRouter(prefix="/api/v1/events", dependencies=[Depends(require_admin_token)])

# Semantic event types eligible for streaming (plan §6-D2 recommended set).
# Everything here is a material state change; heartbeat/token telemetry never
# appears in agent_events, but the allow-list also guards future accidents.
SSE_EVENT_TYPES = (
    "UNREACHABLE",
    "RUNTIME_RECOVERY_EXHAUSTED",
    "RECONCILE_FAILED",
    "EXECUTION_DISPATCHED",
    "EXECUTION_FAILED",
    "EXECUTION_REJECTED",
    "HANDOFF_HUMAN_INPUT",
    "APPROVAL_REQUESTED",
    "STATE_SYNC_FAILED",
    "BUDGET_THRESHOLD_CROSSED",
    "BUDGET_OVERRIDE",
    "SESSION_ROTATION_REQUIRED",
    "RUNTIME_STOPPED_WITH_ACTIVE_WORK",
)

HEARTBEAT_S = 15
POLL_S = 2.0


def _sse_line(event_id: int, event_type: str, payload: dict) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


async def _event_stream(request: Request, last_sequence: int,
                        max_events: int | None = None) -> AsyncIterator[str]:
    from .db import SessionLocal

    yield ": connected (ACMS semantic event stream)\n\n"
    cursor = last_sequence
    sent = 0
    while True:
        if await request.is_disconnected():
            return
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(AgentEventRecord)
                .where(AgentEventRecord.sequence.isnot(None))
                .where(AgentEventRecord.sequence > cursor)
                .where(AgentEventRecord.event_type.in_(SSE_EVENT_TYPES))
                .order_by(AgentEventRecord.sequence.asc())
                .limit(100)
            )).scalars().all()
            if max_events is not None and not rows:
                return  # bounded consumer drained; close cleanly
            for r in rows:
                cursor = r.sequence
                sent += 1
                yield _sse_line(r.sequence, r.event_type, {
                    "event_id": r.event_id,
                    "sequence": r.sequence,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "event_type": r.event_type,
                    "agent_id": r.agent_id,
                    "work_key": r.work_key,
                    "assignment_key": r.assignment_key,
                    "a2a_task_id": r.a2a_task_id,
                    "summary": r.summary,
                })
                if max_events is not None and sent >= max_events:
                    return
        yield f": keepalive {cursor}\n\n"
        await asyncio.sleep(POLL_S)


@router.get("/stream")
async def event_stream(
    request: Request,
    last_id: int = Query(default=0, ge=0),
    max_events: int | None = Query(default=None, ge=1, le=1000),
    _: None = Depends(require_admin_token),
) -> StreamingResponse:
    """Subscribe to ordered ACMS semantic events (SSE).

    Reconnect support: pass ``Last-Event-ID: <sequence>`` header (or
    ``?last_id=<sequence>``) to resume AFTER that sequence — replay covers the
    disconnect window exactly once (durable log ordering guarantees no gaps
    and no duplicates). ``max_events`` optionally bounds delivery (useful for
    polling-style consumers and tests); browsers omit it for a live stream.
    """
    # header wins over query param (browser EventSource sends the header)
    resume = request.headers.get("last-event-id")
    try:
        cursor = int(resume) if resume else last_id
    except ValueError:
        cursor = last_id

    return StreamingResponse(
        _event_stream(request, cursor, max_events=max_events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: stream, don't buffer
            "Connection": "keep-alive",
        },
    )
