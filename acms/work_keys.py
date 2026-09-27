"""Immutable human-readable work/assignment keys (ACMS-REQ-052; ADR-0010).

Allocation is a durable, transactional counter — never a row count — so keys
survive deletes and concurrent transactions. Backed by the ``acms_key_counters``
table (portable across SQLite and PostgreSQL; a PostgreSQL sequence would also
satisfy the requirement but the counter table keeps the unit-test path and the
production path identical).

Format: ``ACMS-WORK-000001`` / ``ACMS-ASG-000001``. Keys are never reused:
the counter only ever increments, even when rows are deleted.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

WORK_PREFIX = "ACMS-WORK-"
ASG_PREFIX = "ACMS-ASG-"


async def _next_counter(db: AsyncSession, name: str) -> int:
    """Atomically increment and return the counter for ``name``."""
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
    value = row.scalar_one()
    return int(value)


async def allocate_work_key(db: AsyncSession) -> str:
    n = await _next_counter(db, "work_key")
    return f"{WORK_PREFIX}{n:06d}"


async def allocate_assignment_key(db: AsyncSession) -> str:
    n = await _next_counter(db, "assignment_key")
    return f"{ASG_PREFIX}{n:06d}"


def extract_work_key_from_title(title: str | None) -> str | None:
    """Find an ACMS-WORK-###### key anywhere in a harness session title."""
    if not title:
        return None
    import re

    m = re.search(r"ACMS-WORK-\d{6,}", title)
    return m.group(0) if m else None
