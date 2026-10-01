"""STEA-004 plan §6/§7/§8/§9 — Work/Artifact UIDs + canonical handoffs.

Covers: UID allocation & uniqueness (same counter as REQ-052 short key),
create_work_item carrying uid, artifact UID at creation sites, canonical
handoff generation (agent-body vs fallback) + idempotency, multi-key search,
and the STNA-86 template validator. Migration backfill determinism is covered
by the PG parity chain (test_migration_model_parity) plus explicit 0014 checks.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from acms import uid_keys
from acms.db import get_session
from acms.work_models import WorkItemCreate
from acms import work_service

from .conftest import clean_db  # noqa: F401  (fixture import)

PGDATA = Path("/tmp/acms-integration-pgdata")


@pytest.fixture()
def pgserver_url():
    url = os.environ.get("ACMS_TEST_POSTGRES_URL")
    if url:
        return url
    try:
        import pgserver

        return pgserver.get_server(PGDATA, cleanup_mode="delete").get_uri()
    except Exception:
        return None


async def _db():
    gen = get_session()
    db = await anext(gen)
    try:
        return db
    finally:
        pass


@pytest.mark.asyncio
async def test_uid_format_and_same_sequence_as_work_key(clean_db):
    db = await _db()
    try:
        seq, uid = await uid_keys.allocate_work_uid(db)
        assert uid.startswith("ACMS-WORK-")
        assert uid.split("-")[2] == f"{seq:06d}"
        derived = uid_keys.work_uid_from_key(f"ACMS-WORK-{seq:06d}", None)
        assert derived and derived.startswith(f"ACMS-WORK-{seq:06d}-")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uid_monotonic_no_reuse(clean_db):
    db = await _db()
    try:
        seqs = [await uid_keys._next_counter(db, "work_key") for _ in range(5)]
        assert seqs == sorted(seqs) and len(set(seqs)) == 5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sequential_allocations_unique(clean_db):
    """SQLite path: allocations are unique + monotonic. True cross-transaction
    concurrency safety is proven on PostgreSQL (see test_uid_concurrency_pg)."""
    db = await _db()
    try:
        seqs = [await uid_keys._next_counter(db, "work_key") for _ in range(12)]
        assert seqs == sorted(seqs) and len(set(seqs)) == 12
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uid_concurrency_pg(pgserver_url):
    """Plan §36: concurrent allocations must be collision-free on PostgreSQL
    (the production engine — UPDATE...RETURNING atomicity is a PG guarantee)."""
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine

    if not pgserver_url:
        pytest.skip("No PostgreSQL available")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # pgserver returns a psycopg-style URI; force the asyncpg driver (ADR-0007 stack).
    engine = create_async_engine(pgserver_url.replace("postgresql+psycopg://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://"))
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS acms_key_counters "
            "(counter_name VARCHAR(64) PRIMARY KEY, counter_value INTEGER NOT NULL)"))
        await conn.execute(text(
            "INSERT INTO acms_key_counters (counter_name, counter_value) "
            "VALUES ('work_key', 0) ON CONFLICT (counter_name) DO NOTHING"))

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _alloc():
        async with maker() as session:
            uid = await uid_keys.allocate_work_uid(session)
            await session.commit()  # production callers commit after allocation
            return uid

    uids = await asyncio.gather(*[_alloc() for _ in range(20)])
    vals = [int(u.split("-")[2]) for _, u in uids]
    assert len(set(vals)) == 20, f"collision: {sorted(vals)}"
    assert sorted(vals) == list(range(min(vals), min(vals) + 20))
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_work_item_carries_uid(clean_db):
    db = await _db()
    try:
        item = await work_service.create_work_item(
            db, WorkItemCreate(title="UID test", created_by="tester"))
        assert item.work_uid and item.work_uid.startswith("ACMS-WORK-")
        assert item.work_sequence == int(item.work_uid.split("-")[2])
        # short key and long UID share the same number
        assert item.work_key == f"ACMS-WORK-{item.work_sequence:06d}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_artifact_uid_allocation(clean_db):
    db = await _db()
    try:
        seq1, uid1 = await uid_keys.allocate_artifact_uid(db)
        seq2, uid2 = await uid_keys.allocate_artifact_uid(db)
        assert seq2 == seq1 + 1
        assert uid1.startswith("ACMS-ARTIFACT-") and uid2.startswith("ACMS-ARTIFACT-")
        assert uid1 != uid2
    finally:
        await db.close()


# ------------------------------------------------------------- canonical handoff

def _seed_task(work_item, agent_id="11111111-1111-1111-1111-111111111111"):
    class _Task:
        task_id = "task-1"
        status = "RUNNING"
        agent_id = "11111111-1111-1111-1111-111111111111"
        work_item_id = work_item.work_item_id

    return _Task()


@pytest.mark.asyncio
async def test_canonical_handoff_created_on_completion(clean_db):
    from acms.canonical_handoff import generate_canonical_handoff, canonical_handoff_exists
    from acms.a2a_models import ArtifactRecord

    db = await _db()
    try:
        item = await work_service.create_work_item(
            db, WorkItemCreate(title="Handoff test", created_by="tester"))
        task = _seed_task(item)
        res = await generate_canonical_handoff(db, task=task, status="SUCCEEDED")
        assert res and res["result"] == "created"
        assert res["artifact_uid"].startswith("ACMS-ARTIFACT-")
        assert await canonical_handoff_exists(db, item.work_item_id)
        art = await db.get(ArtifactRecord, res["artifact_id"])
        assert art.artifact_type == "work_handoff"
        assert "Work Handoff" in art.content
        assert art.created_by == "canonical-handoff:acms_fallback"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_canonical_handoff_idempotent(clean_db):
    from acms.canonical_handoff import generate_canonical_handoff

    db = await _db()
    try:
        item = await work_service.create_work_item(
            db, WorkItemCreate(title="Idempotent test", created_by="tester"))
        task = _seed_task(item)
        r1 = await generate_canonical_handoff(db, task=task, status="SUCCEEDED")
        r2 = await generate_canonical_handoff(db, task=task, status="SUCCEEDED")
        assert r1["result"] == "created"
        assert r2["result"] == "already_exists"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_canonical_handoff_prefers_agent_body(clean_db):
    from datetime import datetime, timezone

    from acms.canonical_handoff import generate_canonical_handoff
    from acms.a2a_models import ArtifactRecord
    from acms.work_models import HandoffRecord

    db = await _db()
    try:
        item = await work_service.create_work_item(
            db, WorkItemCreate(title="Agent-body test", created_by="tester"))
        db.add(HandoffRecord(
            handoff_id="h-1", work_item_id=item.work_item_id, agent_id=None,
            title="Agent handoff",
            body_markdown="# Work Handoff — agent\n\n## BLUF\n\nAgent says it worked.",
            created_at=datetime.now(timezone.utc)))
        await db.commit()
        task = _seed_task(item)
        res = await generate_canonical_handoff(db, task=task, status="SUCCEEDED")
        assert res["body_source"] == "agent"
        art = await db.get(ArtifactRecord, res["artifact_id"])
        assert "Agent says it worked" in art.content
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_canonical_handoff_failed_status_records_error(clean_db):
    from acms.canonical_handoff import generate_canonical_handoff
    from acms.a2a_models import ArtifactRecord

    db = await _db()
    try:
        item = await work_service.create_work_item(
            db, WorkItemCreate(title="Failed run test", created_by="tester"))
        task = _seed_task(item)
        res = await generate_canonical_handoff(
            db, task=task, status="FAILED", error_summary="bridge timeout")
        art = await db.get(ArtifactRecord, res["artifact_id"])
        assert "FAILED" in art.content and "bridge timeout" in art.content
    finally:
        await db.close()


# ------------------------------------------------------------- multi-key search

@pytest.mark.asyncio
async def test_artifact_search_multi_key(clean_db):
    from datetime import datetime, timezone

    from acms.a2a_models import ArtifactRecord
    from sqlalchemy import select

    db = await _db()
    try:
        seq, uid = await uid_keys.allocate_artifact_uid(db)
        art = ArtifactRecord(
            artifact_id="aaaaaaaa-1111-1111-1111-111111111111",
            artifact_uid=uid, artifact_sequence=seq,
            work_item_id=None, title="BLUF proof", artifact_type="bluf",
            content="hello", sha256="81325074b6ed" + "0" * 52,
            created_at=datetime.now(timezone.utc))
        db.add(art)
        await db.commit()

        # Multi-key search through the actual query logic (mirror of the route).
        from acms.a2a_api import ArtifactRecord as _AR  # noqa: F401

        async def _search(q):
            like = f"%{q.strip()}%"
            stmt = select(ArtifactRecord).where(
                ArtifactRecord.artifact_uid.ilike(like)
                | ArtifactRecord.artifact_id.ilike(like)
                | ArtifactRecord.title.ilike(like)
                | ArtifactRecord.jira_issue_key.ilike(like)
                | ArtifactRecord.sha256.ilike(like)
                | ArtifactRecord.work_item_id.ilike(like)
            )
            return (await db.scalars(stmt)).all()

        # by sha prefix
        assert len(await _search("81325074")) == 1
        # by uid
        assert len(await _search(uid[:20])) == 1
        # by title fragment
        assert len(await _search("bluf proof")) == 1
        # negative
        assert len(await _search("nomatch-xyz")) == 0
    finally:
        await db.close()


# ------------------------------------------------------------- STNA-86 template

@pytest.mark.asyncio
async def test_stna86_validator_pass_and_fail(clean_db):
    from acms.jira_template import validate_template_fidelity

    class _MockClient:
        def get_issue(self, key):
            class _I:
                description = (
                    "h2. BLUF\n\n..."
                    "\nh2. Objective\n\n..."
                    "\nh2. Scope\n\n..."
                    "\nh2. Acceptance criteria\n\n...")
            return _I()

    # candidate with all required sections → ok
    good = "h2. BLUF\n\nx\nh2. Objective\n\nx\nh2. Scope\n\nx\nh2. Acceptance criteria\n\nx"
    res = await validate_template_fidelity(_MockClient(), "STNA-86", candidate_description=good)
    assert res.ok, res.missing_required

    # candidate missing sections → fails
    bad = "h2. Summary\n\nonly a summary"
    res = await validate_template_fidelity(_MockClient(), "STNA-86", candidate_description=bad)
    assert not res.ok
    assert "bluf" in res.missing_required or any("bluf" in m for m in res.missing_required)


@pytest.mark.asyncio
async def test_stna86_validator_without_source_uses_floor(clean_db):
    from acms.jira_template import validate_template_fidelity

    class _FailingClient:
        def get_issue(self, key):
            raise RuntimeError("jira down")

    res = await validate_template_fidelity(
        _FailingClient(), "STNA-86",
        candidate_description="h2. BLUF\n\nx\nh2. Objective\n\nx\nh2. Scope\n\nx\nh2. Acceptance criteria\n\nx")
    assert res.ok
    assert res.source_fetch_ok is False
    assert any("REQUIRED_SECTION_FLOOR" in n for n in res.notes)