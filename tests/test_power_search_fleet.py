"""STEA-004 §17/§19/§27 — power ingest, usage summary, fleet sort/search, global search.

FacilityPowerClient is exercised with a monkeypatched fetch (hermetic);
the stale→NULL-never-0 semantics are asserted at the shaping layer.
"""
from __future__ import annotations

import pytest

from acms import power_ingest as pi
from acms.db import SessionLocal

from .conftest import clean_db  # noqa: F401


async def _db():
    return SessionLocal()


def _upstream_payload(stale=False):
    ch = lambda n, w, k24, k30: {  # noqa: E731
        "channel_num": str(n), "label": f"chan{n}", "avg_watts_1h": w,
        "kwh_24h": k24, "cost_usd_24h": round(k24 * 0.16, 4),
        "kwh_30d": k30, "cost_usd_30d": round(k30 * 0.16, 4),
        "stale": stale, "last_sample_ts": "2026-10-01T23:00:00+00:00"}
    return {
        "error": None,
        "totals": {"label": "TOTAL MARION_IA_USA",
                   "definition": "PDU+PDU+PDU+cooling",
                   "kwh_24h": None if stale else 11.5,
                   "cost_usd_24h": None if stale else 1.84,
                   "kwh_30d": None if stale else 208.0,
                   "cost_usd_30d": None if stale else 33.28,
                   "incomplete_reason": "stale" if stale else None},
        "channels": [ch(1, 100.0, 5.0, 80.0), ch(2, 200.0, 3.0, 60.0),
                     ch(3, 300.0, 2.0, 40.0), ch(4, 50.0, 1.5, 28.0)],
        "collector": {"healthy": not stale, "stale": stale, "samples_1h": 130,
                      "last_sample_ts": "2026-10-01T23:00:00+00:00",
                      "last_sample_age_seconds": 900 if stale else 78,
                      "stale_policy": "stale data shows STALE + timestamp — NEVER rendered as 0"},
        "rate": {"effective_per_kwh": 0.16},
    }


@pytest.mark.asyncio
async def test_power_ingest_fresh(clean_db, monkeypatch):
    db = await _db()
    payload = _upstream_payload(stale=False)
    monkeypatch.setattr(pi.FacilityPowerClient, "fetch", lambda self: payload)
    try:
        out = await pi.ingest_power_snapshot(db)
        assert out["error"] is None
        assert out["facility"]["kwh_24h"] == 11.5
        assert out["facility"]["current_watts"] == 650.0  # 100+200+300+50
        assert len(out["channels"]) == 4
        assert out["collector"]["healthy"] is True
        # stored row
        from acms.power_ingest import PowerSnapshotRecord

        rec = (await db.scalars(
            __import__("sqlalchemy").select(PowerSnapshotRecord)
            .order_by(PowerSnapshotRecord.captured_at.desc()).limit(1))).first()
        assert rec.total_kwh_24h == 11.5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_power_ingest_stale_never_zero(clean_db, monkeypatch):
    db = await _db()
    payload = _upstream_payload(stale=True)
    monkeypatch.setattr(pi.FacilityPowerClient, "fetch", lambda self: payload)
    try:
        out = await pi.ingest_power_snapshot(db)
        # stale → totals NULL (never 0) + reason carried
        assert out["facility"]["kwh_24h"] is None
        assert out["facility"]["incomplete_reason"]
        assert out["collector"]["stale"] is True
        assert out["collector"]["stale_policy"].startswith("stale")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_power_fetch_error_surfaces(clean_db, monkeypatch):
    db = await _db()
    monkeypatch.setattr(pi.FacilityPowerClient, "fetch",
                        lambda self: {"error": "facility API unreachable: timeout"})
    try:
        out = await pi.ingest_power_snapshot(db)
        assert out["error"] and "unreachable" in out["error"]
        assert out["facility"]["kwh_24h"] is None  # no fabricated zeros
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_usage_summary_groups_by_model(clean_db):
    from datetime import datetime, timezone

    from acms.economics_models import CostAttributionRecord
    from sqlalchemy import select

    db = await _db()
    try:
        now = datetime.now(timezone.utc)
        for i, (model, prov, tin, tout, cost) in enumerate([
                ("qwen3.8-flash-next", "local", 1000, 100, 0.0),
                ("deepseek-v4.1-flash", "Together", 500, 50, 0.25)]):
            db.add(CostAttributionRecord(
                entry_id=f"e{i}", outcome_id=f"o{i}", session_id=None,
                model_id=model, provider=prov, input_tokens=tin,
                output_tokens=tout, cached_input_tokens=0,
                api_cost_usd=cost, source="session", failed_run=False,
                created_at=now))
        await db.commit()
        out = await pi.usage_summary(db, hours=24)
        assert len(out["models"]) == 2
        assert out["total_cloud_cost_usd_actual"] == 0.25
        assert out["total_tokens"]["input"] == 1500
        assert out["models"][0]["model_id"] == "deepseek-v4.1-flash"  # sorted by cost desc
        assert "ESTIMATE" in out["note"].upper()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_global_search_groups(clean_db):
    from datetime import datetime, timezone

    from acms.a2a_models import ArtifactRecord, ProductRecord
    from acms import uid_keys
    from acms.work_models import WorkItemRecord
    from acms.search_api import global_search

    db = await _db()
    try:
        now = datetime.now(timezone.utc)
        db.add(WorkItemRecord(
            work_item_id="w-1", work_key="ACMS-WORK-000099",
            work_uid="ACMS-WORK-000099-20261001_080000", work_sequence=99,
            kind="task", title="Deploy the widget service", status="PLANNED",
            scope_markdown="", created_at=now, updated_at=now))
        seq, uid = await uid_keys.allocate_artifact_uid(db)
        db.add(ArtifactRecord(
            artifact_id="art-1", artifact_uid=uid, artifact_sequence=seq,
            title="Widget BLUF", artifact_type="bluf", content="x",
            sha256="abc123" + "0" * 58, created_at=now))
        db.add(ProductRecord(product_id="p-1", name="WidgetLine", slug="widgetline",
                             created_at=now, updated_at=now))
        await db.commit()

        res = await global_search(q="widget", db=db)
        assert res["work"] and res["work"][0]["work_uid"].startswith("ACMS-WORK-")
        assert res["artifacts"] and res["artifacts"][0]["title"] == "Widget BLUF"
        assert res["products"] and res["products"][0]["slug"] == "widgetline"

        res = await global_search(q="ACMS-WORK-000099", db=db)
        assert len(res["work"]) == 1

        res = await global_search(q="abc123", db=db)
        assert len(res["artifacts"]) == 1  # sha prefix finds it
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fleet_search_sort(clean_db):
    from datetime import datetime, timezone

    from acms.models import AgentRecord
    from sqlalchemy import select

    db = await _db()
    try:
        now = datetime.now(timezone.utc)
        for i, (name, uid, harness) in enumerate([
                ("acms-hermes-worker-uid-002", "002", "hermes"),
                ("acms-hermes-worker-uid-001", "001", "hermes"),
                ("acms-codex-worker-uid-003", "003", "codex")]):
            db.add(AgentRecord(
                agent_id=f"0000000{i}-0000-0000-0000-00000000000{i}",
                external_registration_id=f"reg-{i}", display_name=name,
                legacy_name=f"acms-worker-{i+1}", worker_uid=uid,
                trust_class="internal", harness=harness,
                bridge_version="0.19.0", protocol_version="1",
                created_at=now, updated_at=now))
        await db.commit()

        # exercise the same filter/sort logic the route applies (route wrapper
        # only adds auth + session deps — logic identical, no logic drift risk)
        from acms.main import _fleet_rows_filtered

        rows = await _fleet_rows_filtered(db, q="worker-uid", sort="worker_uid", harness=None)
        assert [r.worker_uid for r in rows] == ["001", "002", "003"]
        rows = await _fleet_rows_filtered(db, q=None, sort="-worker_uid", harness=None)
        assert [r.worker_uid for r in rows] == ["003", "002", "001"]
        rows = await _fleet_rows_filtered(db, q=None, sort=None, harness="codex")
        assert len(rows) == 1 and rows[0].harness == "codex"
    finally:
        await db.close()