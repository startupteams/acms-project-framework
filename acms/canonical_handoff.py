"""Canonical Markdown work handoff per completed Work Item (STEA-004 plan §9).

Rule: the Inbox is NOT the handoff. Every terminal Work Item gets exactly one
canonical ``work_handoff`` artifact:

- If the agent provided handoff content (work_handoffs table OR the callback's
  handoff_reference), it becomes the canonical body — the agent's output is
  NEVER overwritten (plan §47: artifact generation never overwrites original
  agent output).
- Otherwise ACMS synthesizes a concise fallback from durable data: work item,
  events trail, execution sessions (model/tokens/cost), artifacts, Jira links.

Idempotent: one canonical handoff per work item (lookup before create); a
second completion signal never duplicates it. Handoff generation NEVER breaks
completion — every failure is swallowed with a durable audit event.

Sections follow plan §9 exactly:
    BLUF / Requested Work / What Was Done / Result / Tests-Evidence /
    Model-Agent-Runtime / Artifacts-Code-PRs / Jira / Costs-Usage /
    Problems-Warnings / Remaining Work
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .uid_keys import allocate_artifact_uid

HANDOFF_TYPE = "work_handoff"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


async def _agent_handoff_body(db: AsyncSession, work_item_id: str) -> str | None:
    """Most recent structured agent handoff for this work item, if any."""
    from .work_models import HandoffRecord

    row = (await db.scalars(
        select(HandoffRecord)
        .where(HandoffRecord.work_item_id == work_item_id)
        .order_by(HandoffRecord.created_at.desc())
        .limit(1)
    )).first()
    return (row.body_markdown or "").strip() or None if row else None


async def canonical_handoff_exists(db: AsyncSession, work_item_id: str) -> bool:
    from .a2a_models import ArtifactRecord

    return (await db.scalars(
        select(ArtifactRecord.artifact_id)
        .where(ArtifactRecord.work_item_id == work_item_id)
        .where(ArtifactRecord.artifact_type == HANDOFF_TYPE)
        .limit(1)
    )).first() is not None


async def generate_canonical_handoff(
    db: AsyncSession,
    *,
    task,
    status: str,
    completed_at: datetime | None = None,
    error_summary: str | None = None,
    usage_reference: dict | None = None,
    handoff_reference: str | None = None,
) -> dict | None:
    """Create the canonical work_handoff artifact for a terminal work item.

    Returns {artifact_id, artifact_uid, reused} or None when generation is
    skipped (no work item, already exists) — never raises.
    """
    try:
        work_item_id = getattr(task, "work_item_id", None)
        if not work_item_id:
            return None  # standalone task with no Work Item: no handoff target

        from .a2a_models import ArtifactRecord
        from .work_models import WorkItemRecord

        work = await db.get(WorkItemRecord, work_item_id)
        if work is None:
            return None
        if await canonical_handoff_exists(db, work_item_id):
            return {"result": "already_exists", "work_item_id": work_item_id}

        completed_at = completed_at or _now()
        agent_id = getattr(task, "agent_id", None)

        # ---- gather durable context ------------------------------------------
        from .memory_models import ExecutionSessionRecord as _ESR

        sessions = (await db.scalars(
            select(_ESR)
            .where(_ESR.work_item_id == work_item_id)
            .order_by(_ESR.started_at.asc())
        )).all()

        artifacts = (await db.scalars(
            select(ArtifactRecord)
            .where(ArtifactRecord.work_item_id == work_item_id)
            .order_by(ArtifactRecord.created_at.asc())
        )).all()

        agent_name = None
        if agent_id:
            from .models import AgentRecord

            agent = await db.get(AgentRecord, agent_id)
            agent_name = getattr(agent, "display_name", None) or getattr(agent, "worker_uid", None)

        # ---- choose canonical body -------------------------------------------
        agent_body = await _agent_handoff_body(db, work_item_id)
        if agent_body:
            body_source = "agent"
            body = agent_body
        else:
            body_source = "acms_fallback"
            body = _fallback_body(
                work=work, status=status, completed_at=completed_at,
                sessions=sessions, artifacts=artifacts, agent_name=agent_name,
                error_summary=error_summary, usage_reference=usage_reference,
                handoff_reference=handoff_reference,
            )

        bluf = _extract_bluf(body) or (
            f"Work {work.work_uid or work.work_item_id} finished with status {status}."
        )

        _seq, uid = await allocate_artifact_uid(db, completed_at)
        art = ArtifactRecord(
            artifact_id=ArtifactRecord.new_id(),
            artifact_uid=uid, artifact_sequence=_seq,
            work_item_id=work_item_id, agent_id=agent_id,
            project_id=work.project_id, product_id=None,
            jira_issue_key=work.jira_issue_key,
            artifact_type=HANDOFF_TYPE,
            title=f"Work Handoff — {work.work_uid or work.work_item_id}",
            bluf=bluf[:512],
            content=body,
            sha256=ArtifactRecord.sha(body),
            created_at=completed_at, created_by=f"canonical-handoff:{body_source}",
        )
        db.add(art)

        # Durable audit event (§35 traceability)
        from .telemetry_service import add_event

        await add_event(
            db, event_type="WORK_HANDOFF_GENERATED", actor_source="canonical_handoff",
            agent_id=agent_id, work_key=work.work_key,
            summary=f"Canonical work handoff {uid} created ({body_source})",
            metadata={"work_item_id": work_item_id, "artifact_id": art.artifact_id,
                      "artifact_uid": uid, "body_source": body_source, "status": status},
        )
        return {"result": "created", "artifact_id": art.artifact_id,
                "artifact_uid": uid, "body_source": body_source}
    except Exception as exc:  # noqa: BLE001 — handoff generation never breaks completion
        try:
            from .telemetry_service import add_event

            await add_event(
                db, event_type="WORK_HANDOFF_GENERATION_FAILED",
                actor_source="canonical_handoff", agent_id=None, work_key=None,
                summary=f"Canonical handoff generation failed: {str(exc)[:200]}",
                metadata={"work_item_id": str(getattr(task, "work_item_id", None))},
            )
        except Exception:  # noqa: BLE001
            pass
        return None


def _extract_bluf(body: str) -> str | None:
    """Pull the BLUF section's first paragraph from a Markdown body."""
    lines = body.splitlines()
    in_bluf = False
    buf: list[str] = []
    for ln in lines:
        if ln.strip().lower().startswith("#") and "bluf" in ln.lower():
            in_bluf = True
            continue
        if in_bluf:
            if ln.strip().startswith("#"):
                break
            if ln.strip():
                buf.append(ln.strip())
    return " ".join(buf)[:400] or None


def _fallback_body(
    *, work, status: str, completed_at, sessions, artifacts, agent_name,
    error_summary, usage_reference, handoff_reference,
) -> str:
    parts: list[str] = []
    uid = work.work_uid or work.work_key or work.work_item_id
    parts.append(f"# Work Handoff — {uid}")
    parts.append("")
    parts.append("## BLUF")
    if status == "SUCCEEDED":
        parts.append(f"Work completed successfully at {_fmt_ts(completed_at)}.")
    elif status == "FAILED":
        parts.append(f"Work FAILED at {_fmt_ts(completed_at)}."
                     + (f" Error: {error_summary}" if error_summary else ""))
    else:
        parts.append(f"Work terminal at {_fmt_ts(completed_at)} with status {status}.")
    parts.append("")
    parts.append("## Requested Work")
    parts.append(f"- **Title:** {work.title}")
    if work.scope_markdown:
        parts.append(work.scope_markdown[:3000])
    else:
        parts.append("- (no scope recorded)")
    parts.append("")
    parts.append("## What Was Done")
    if sessions:
        for s in sessions:
            bits = [f"- Session `{s.session_id[:8]}` — status {s.status}"]
            if s.model_id:
                bits.append(f"model `{s.model_id}`")
            if s.a2a_task_id:
                bits.append(f"A2A run `{s.a2a_task_id[:24]}`")
            parts.append(" — ".join(bits))
    else:
        parts.append("- (no execution sessions recorded)")
    parts.append("")
    parts.append("## Result")
    parts.append(f"- Terminal status: **{status}** at {_fmt_ts(completed_at)}")
    if handoff_reference:
        parts.append(f"- Agent handoff reference: `{handoff_reference[:200]}`")
    parts.append("")
    parts.append("## Tests / Evidence")
    ev_bits = []
    for a in artifacts:
        if a.artifact_type == HANDOFF_TYPE:
            continue  # this handoff itself
        ev_bits.append(f"- [{a.title}](/ui/artifacts/{a.artifact_uid or a.artifact_id}) "
                       f"({a.artifact_type}, sha {a.sha256[:8]})")
    parts.extend(ev_bits or ["- (no evidence artifacts recorded)"])
    parts.append("")
    parts.append("## Model / Agent / Runtime")
    models = sorted({s.model_id for s in sessions if s.model_id})
    parts.append(f"- Agent: {agent_name or 'unknown'}")
    parts.append(f"- Model(s): {', '.join(models) or 'UNKNOWN (never fabricated)'}")
    parts.append("")
    parts.append("## Artifacts / Code / PRs")
    if artifacts:
        for a in artifacts:
            if a.artifact_type == HANDOFF_TYPE:
                continue
            parts.append(f"- {a.artifact_uid or a.artifact_id}: {a.title}")
    else:
        parts.append("- (none)")
    parts.append("")
    parts.append("## Jira")
    if work.jira_issue_key:
        parts.append(f"- [{work.jira_issue_key}]({work.jira_url or '#'})")
    else:
        parts.append("- (not linked)")
    parts.append("")
    parts.append("## Costs / Usage")
    total_in = total_out = 0
    for s in sessions:
        total_in += s.cumulative_api_tokens or 0
    u = usage_reference or {}
    if u.get("input_tokens"):
        total_in += u["input_tokens"]
    if u.get("output_tokens"):
        total_out += u["output_tokens"]
    if total_in or total_out:
        parts.append(f"- Tokens (durable records): in {total_in:,} / out {total_out:,}")
    costs = sorted({s.estimated_cloud_cost_usd for s in sessions
                    if s.estimated_cloud_cost_usd is not None})
    local = sorted({s.estimated_local_cost_usd for s in sessions
                    if s.estimated_local_cost_usd is not None})
    if costs or local:
        parts.append(f"- Cloud (est.): {costs} · Local (est.): {local}")
    else:
        parts.append("- (no cost records on sessions)")
    parts.append("")
    parts.append("## Problems / Warnings")
    if error_summary:
        parts.append(f"- {error_summary}")
    else:
        parts.append("- (none recorded)")
    parts.append("")
    parts.append("## Remaining Work")
    parts.append("- (human completes this section as needed)")
    return "\n".join(parts)