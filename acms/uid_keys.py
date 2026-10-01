"""Human-readable immutable UIDs (STEA-004 plan §6/§7; ADR-0017 identity rules).

Two UID families, both backed by the durable ``acms_key_counters`` table
(never COUNT(*), never reused — the counter only increments):

- Work UID:  ``ACMS-WORK-######-YYYYMMDD_HHMMSS`` (§6)
- Artifact UID: ``ACMS-ARTIFACT-######-YYYYMMDD_HHMMSS`` (§7)

Design decisions (recorded for reviewers):

- Work UIDs share the SAME ``work_key`` counter as the short REQ-052 key so
  ``ACMS-WORK-000127`` and ``ACMS-WORK-000127-20261001_082241`` are the same
  number for the same row — session-alignment machinery (telemetry_service
  extracts short keys from harness titles) stays compatible, and the UID is
  a pure extension of the existing identity rather than a second identity.
- Artifact UIDs use a NEW ``artifact_uid`` counter (artifacts had no
  human-readable key before this release).
- The timestamp portion is the row's ``created_at`` rendered in UTC —
  computed ONCE at allocation; the full UID is immutable afterwards.
- Internal UUIDs remain the immutable database identity (REQ-001 discipline).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

WORK_UID_PREFIX = "ACMS-WORK-"
ARTIFACT_UID_PREFIX = "ACMS-ARTIFACT-"
COUNTER_WORK = "work_key"  # shared with the REQ-052 short key (same sequence)
COUNTER_ARTIFACT = "artifact_uid"


def _ts_portion(ts: datetime | None) -> str:
    """created_at rendered as YYYYMMDD_HHMMSS in UTC (naive == treated as UTC)."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


def format_uid(prefix: str, seq: int, ts: datetime | None) -> str:
    return f"{prefix}{seq:06d}-{_ts_portion(ts)}"


async def _next_counter(db: AsyncSession, name: str) -> int:
    """Atomically increment and return the counter (same mechanism as work_keys)."""
    await db.execute(
        text(
            "INSERT INTO acms_key_counters (counter_name, counter_value) "
            "VALUES (:name, 0) ON CONFLICT (counter_name) DO NOTHING"
        ),
        {"name": name},
    )
    row = await db.execute(
        text(
            "UPDATE acms_key_counters SET counter_value = counter_value + 1 "
            "WHERE counter_name = :name RETURNING counter_value"
        ),
        {"name": name},
    )
    return int(row.scalar_one())


async def allocate_work_uid(db: AsyncSession, created_at: datetime | None = None) -> tuple[int, str]:
    """Allocate the next work sequence and return ``(seq, full_uid)``."""
    seq = await _next_counter(db, COUNTER_WORK)
    return seq, format_uid(WORK_UID_PREFIX, seq, created_at)


async def allocate_artifact_uid(db: AsyncSession, created_at: datetime | None = None) -> tuple[int, str]:
    """Allocate the next artifact sequence and return ``(seq, full_uid)``."""
    seq = await _next_counter(db, COUNTER_ARTIFACT)
    return seq, format_uid(ARTIFACT_UID_PREFIX, seq, created_at)


def work_uid_from_key(work_key: str | None, created_at: datetime | None) -> str | None:
    """Derive the long-form UID from an existing short key (backfill path).

    ``ACMS-WORK-000127`` + created_at → ``ACMS-WORK-000127-20261001_082241``.
    Returns None when no short key exists (caller must allocate instead).
    """
    if not work_key or not work_key.startswith(WORK_UID_PREFIX):
        return None
    seq_part = work_key[len(WORK_UID_PREFIX):]
    if not seq_part.isdigit():
        return None
    return format_uid(WORK_UID_PREFIX, int(seq_part), created_at)