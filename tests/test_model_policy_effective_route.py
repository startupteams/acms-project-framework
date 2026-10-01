"""Regression (live-found 2026-10-01): /model-policy/effective/{work_item_id}
was UNREACHABLE — the dynamic /model-policy/{scope}/{scope_id} route was
declared FIRST, so FastAPI shadowed the effective route (scope='effective').
Any effective-policy lookup returned 404 'no policy for scope'.

Fix: declare the effective route BEFORE the dynamic scope route (and remove
the duplicate declaration that lived after it)."""
from __future__ import annotations

import pytest

from .test_a2a_production_path import AUTH, client  # noqa: F401  (fixtures)


@pytest.mark.asyncio
async def test_effective_policy_route_not_shadowed(client, clean_db):
    from sqlalchemy import select

    from acms import model_policy as mp
    from acms.a2a_models import ModelPolicyRecord
    from acms import work_service
    from acms.work_models import WorkItemCreate

    gen = __import__("acms.db", fromlist=["get_session"]).get_session()
    db = await anext(gen)
    try:
        existing = (await db.scalars(
            select(ModelPolicyRecord).where(ModelPolicyRecord.scope == "system"))).first()
        if existing is None:
            await mp.upsert_policy(db, scope="system", scope_id=None,
                                   inference_policy="local-preferred",
                                   preferred_model="qwen3.8-flash-next",
                                   cloud_fallback=True, updated_by="test")
        work = await work_service.create_work_item(db, WorkItemCreate(title="route probe"))
    finally:
        await db.close()

    r = client.get(f"/model-policy/effective/{work.work_item_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preferred_model"] == "qwen3.8-flash-next"
    assert body["resolution_reason"] == "system"

    # the dynamic scope route still works for a real scope
    rs = client.get("/model-policy/system/system", headers=AUTH)
    assert rs.status_code == 200
    assert rs.json()["scope"] == "system"
