"""Human Inbox service (ADR-0017; STEA-004 plan §23/§24).

Durable actionable-inbox rows derived from + linked to execution context.
Item classes: ACTION_REQUIRED (needs a human decision), FYI (informational),
STALE (auto-demoted when the linked execution resolves or the trigger
becomes irrelevant). Severity is separate from class. Related events within
a short window aggregate via correlation_id — never suppress critical items.

Caveman-style summary discipline: `summary` <= 120 chars, short declarative
sentences, no jargon (plan §23 inbox list format).
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .a2a_models import HumanInboxItemRecord

ITEM_CLASSES = ("ACTION_REQUIRED", "FYI", "STALE")
SEVERITIES = ("critical", "high", "medium", "low", "info")


def _caveman_summary(text: str) -> str:
    """Shorten to the first 2-3 sentences, hard cap 120 chars."""
    text = " ".join((text or "").split())
    if len(text) <= 120:
        return text
    cut = text[:120]
    # break at the last sentence boundary inside the cap when possible
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > 40:
            return cut[: idx + 1]
    return cut[:117] + "..."


async def create_inbox_item(
    db: AsyncSession, *, title: str, summary: str,
    item_class: str = "ACTION_REQUIRED", severity: str = "medium",
    agent_id: str | None = None, work_item_id: str | None = None,
    execution_session_id: str | None = None, project_id: str | None = None,
    product_id: str | None = None, jira_issue_key: str | None = None,
    correlation_id: str | None = None, metadata: dict[str, Any] | None = None,
) -> HumanInboxItemRecord:
    """Create one inbox row. correlation_id dedupes an OPEN item of the same
    trigger (related events aggregate into one item per plan §23)."""
    if item_class not in ITEM_CLASSES:
        raise ValueError(f"unknown inbox class {item_class!r}")
    if severity not in SEVERITIES:
        severity = "medium"
    if correlation_id:
        existing = (await db.scalars(
            select(HumanInboxItemRecord).where(
                HumanInboxItemRecord.correlation_id == correlation_id,
                HumanInboxItemRecord.archived_at.is_(None),
            ).order_by(HumanInboxItemRecord.created_at.desc()).limit(1)
        )).first()
        if existing is not None:
            # aggregate: refresh the summary/timestamp, keep one row
            existing.summary = _caveman_summary(summary) or existing.summary
            existing.updated_at = HumanInboxItemRecord.now()
            if metadata:
                try:
                    merged = json.loads(existing.metadata_json or "{}")
                except (TypeError, ValueError):
                    merged = {}
                merged.update(metadata)
                existing.metadata_json = json.dumps(merged)
            await db.commit()
            await db.refresh(existing)
            return existing
    rec = HumanInboxItemRecord(
        item_id=HumanInboxItemRecord.new_id(),
        item_class=item_class,
        severity=severity,
        title=title[:255],
        summary=_caveman_summary(summary),
        agent_id=agent_id,
        work_item_id=work_item_id,
        execution_session_id=execution_session_id,
        project_id=project_id,
        product_id=product_id,
        jira_issue_key=jira_issue_key,
        correlation_id=correlation_id,
        metadata_json=json.dumps(metadata) if metadata else None,
        created_at=HumanInboxItemRecord.now(),
        updated_at=HumanInboxItemRecord.now(),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def demote_for_work_item(
    db: AsyncSession, work_item_id: str, *, to_class: str = "STALE",
    reason: str = "linked execution resolved",
) -> int:
    """Auto-demotion (plan §23): ACTION_REQUIRED items whose execution finished
    or found another path become FYI/STALE. Returns demoted count."""
    if to_class not in ITEM_CLASSES:
        raise ValueError(f"unknown inbox class {to_class!r}")
    rows = (await db.scalars(
        select(HumanInboxItemRecord).where(
            HumanInboxItemRecord.work_item_id == work_item_id,
            HumanInboxItemRecord.item_class == "ACTION_REQUIRED",
            HumanInboxItemRecord.archived_at.is_(None),
        )
    )).all()
    now = HumanInboxItemRecord.now()
    for r in rows:
        r.item_class = to_class
        r.updated_at = now
        r.metadata_json = json.dumps({"demoted_reason": reason, **_safe_json(r.metadata_json)})
    if rows:
        await db.commit()
    return len(rows)


def _safe_json(raw: str | None) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


async def list_inbox(
    db: AsyncSession, *, unread_only: bool = False, item_class: str | None = None,
    severity: str | None = None, agent_id: str | None = None,
    project_id: str | None = None, include_archived: bool = False,
    search: str | None = None, limit: int = 100,
) -> list[HumanInboxItemRecord]:
    stmt = select(HumanInboxItemRecord).order_by(HumanInboxItemRecord.created_at.desc())
    if not include_archived:
        stmt = stmt.where(HumanInboxItemRecord.archived_at.is_(None))
    if unread_only:
        stmt = stmt.where(HumanInboxItemRecord.read_at.is_(None))
    if item_class:
        stmt = stmt.where(HumanInboxItemRecord.item_class == item_class)
    if severity:
        stmt = stmt.where(HumanInboxItemRecord.severity == severity)
    if agent_id:
        stmt = stmt.where(HumanInboxItemRecord.agent_id == agent_id)
    if project_id:
        stmt = stmt.where(HumanInboxItemRecord.project_id == project_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            HumanInboxItemRecord.title.ilike(like) | HumanInboxItemRecord.summary.ilike(like)
        )
    stmt = stmt.limit(min(limit, 300))
    return list((await db.scalars(stmt)).all())


async def mark_read(db: AsyncSession, item_id: str, *, read: bool = True) -> HumanInboxItemRecord | None:
    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        return None
    rec.read_at = None if not read else (rec.read_at or HumanInboxItemRecord.now())
    rec.updated_at = HumanInboxItemRecord.now()
    await db.commit()
    await db.refresh(rec)
    return rec


async def archive_item(db: AsyncSession, item_id: str, *, force: bool = False) -> tuple[HumanInboxItemRecord | None, str | None]:
    """Archive. Unresolved ACTION_REQUIRED requires force=True (plan §23)."""
    rec = await db.get(HumanInboxItemRecord, item_id)
    if rec is None:
        return None, "unknown-item"
    if rec.archived_at is not None:
        return rec, None
    if rec.item_class == "ACTION_REQUIRED" and rec.read_at is None and not force:
        return None, "unresolved-action-required"
    rec.archived_at = HumanInboxItemRecord.now()
    rec.archived_force = bool(force)
    rec.updated_at = HumanInboxItemRecord.now()
    await db.commit()
    await db.refresh(rec)
    return rec, None