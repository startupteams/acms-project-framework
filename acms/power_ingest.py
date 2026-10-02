"""ACMS facility power ingestion (STEA-004 plan §17).

LLM Manager remains the collector/provider; ACMS pulls the NORMALIZED view
from the Server Manager machine API (GET /api/v1/facility/power, scoped
service token — the same svc-acms credential ACMS already holds for runtime
ops). No Emporia credentials and no llmmanager DSN ever reach ACMS.

Semantics preserved end-to-end (§17 + §40):
- stale channels/collector → value NULL + STALE + timestamp (NEVER 0)
- TOTAL MARION_IA_USA withheld when any channel stale (reason carried)
- local inference attribution stays "estimated" and never exceeds measured
  relevant energy (usage rollups are reported separately, labeled estimated)

ACMS becomes the primary human view: /power/summary + /usage/summary
(usage summary aggregates the existing cost_attribution rows — §17 breakdowns
by model/provider/agent where attribution supports it).
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Base
from .settings import get_settings

# ---------------------------------------------------------------- ORM model

from sqlalchemy import Float, Integer, String, DateTime  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402


class PowerSnapshotRecord(Base):
    """Normalized interval summaries ingested from LLM Manager (§32)."""

    __tablename__ = "power_cost_snapshot"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(32), default="llm-manager-facility")
    # raw upstream payload (JSON) for audit/replay
    payload_json: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # facility aggregates (NULL when stale — never 0)
    total_kwh_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost_usd_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_kwh_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost_usd_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    collector_stale: Mapped[bool | None] = mapped_column(nullable=True)
    last_sample_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())


# ---------------------------------------------------------------- fetch


class FacilityPowerClient:
    """Server Manager /api/v1/facility/power client (scoped service token)."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = 15):
        s = get_settings()
        self.base_url = (base_url or s.server_manager_base_url or "").rstrip("/")
        self.token = token or s.server_manager_token or ""
        self.timeout = timeout

    def fetch(self) -> dict:
        if not self.base_url or not self.token:
            return {"error": "server manager not configured"}
        req = urllib.request.Request(f"{self.base_url}/api/v1/facility/power")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode())
            except Exception:  # noqa: BLE001
                detail = {}
            return {"error": f"facility API {e.code}: {str(detail.get('detail', ''))[:120]}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"facility API unreachable: {str(exc)[:120]}"}


async def ingest_power_snapshot(db: AsyncSession) -> dict:
    """Fetch + store one snapshot. Returns the summary the API responds with."""
    client = FacilityPowerClient()
    payload = client.fetch()
    now = datetime.now(timezone.utc)
    totals = payload.get("totals") or {}
    collector = payload.get("collector") or {}
    channels = payload.get("channels") or []
    current_watts = None
    last_ts = None
    try:
        watts = [c.get("avg_watts_1h") for c in channels]
        if watts and not any(c.get("stale") for c in channels):
            current_watts = round(sum(float(w or 0) for w in watts), 1)
        tses = [c.get("last_sample_ts") for c in channels if c.get("last_sample_ts")]
        if tses:
            last_ts = max(datetime.fromisoformat(t) for t in tses)
    except Exception:  # noqa: BLE001 — parse failures never crash ingest
        pass
    rec = PowerSnapshotRecord(
        snapshot_id=PowerSnapshotRecord.new_id(),
        captured_at=now, source="llm-manager-facility",
        # Phase-C fix: persist the FULL payload (incl. channels + rate +
        # collector detail) — latest_power_summary() reconstructs the §17
        # view from THIS payload, so dropping channels made the stored
        # snapshot render totals-only while ?refresh=1 showed channels.
        payload_json=json.dumps({"error": payload.get("error"), "totals": totals,
                                 "channels": channels, "rate": payload.get("rate", {}),
                                 "collector": collector})[:4000],
        total_kwh_24h=totals.get("kwh_24h"),
        total_cost_usd_24h=totals.get("cost_usd_24h"),
        total_kwh_30d=totals.get("kwh_30d"),
        total_cost_usd_30d=totals.get("cost_usd_30d"),
        current_watts=current_watts,
        collector_stale=bool(collector.get("stale")) if collector else None,
        last_sample_ts=last_ts,
    )
    db.add(rec)
    await db.commit()
    return _summary_from(payload, rec)


def _summary_from(payload: dict, rec: PowerSnapshotRecord) -> dict:
    """Shape the API response (§17): stale → STALE + timestamp + NULL, never 0."""
    channels = payload.get("channels") or []
    collector = payload.get("collector") or {}
    totals = payload.get("totals") or {}
    return {
        "error": payload.get("error"),
        "captured_at": rec.captured_at,
        "facility": {
            "label": totals.get("label", "TOTAL MARION_IA_USA"),
            "definition": totals.get("definition"),
            "current_watts": rec.current_watts,
            "kwh_24h": totals.get("kwh_24h"),
            "cost_usd_24h": totals.get("cost_usd_24h"),
            "kwh_30d": totals.get("kwh_30d"),
            "cost_usd_30d": totals.get("cost_usd_30d"),
            "incomplete_reason": totals.get("incomplete_reason"),
        },
        "channels": [{
            "label": c.get("label"), "channel_num": c.get("channel_num"),
            "avg_watts_1h": c.get("avg_watts_1h"),
            "kwh_24h": c.get("kwh_24h"), "cost_usd_24h": c.get("cost_usd_24h"),
            "kwh_30d": c.get("kwh_30d"), "cost_usd_30d": c.get("cost_usd_30d"),
            "stale": c.get("stale"), "last_sample_ts": c.get("last_sample_ts"),
        } for c in channels],
        "collector": {
            "healthy": collector.get("healthy"), "stale": collector.get("stale"),
            "samples_1h": collector.get("samples_1h"),
            "last_sample_ts": collector.get("last_sample_ts"),
            "last_sample_age_seconds": collector.get("last_sample_age_seconds"),
            "stale_policy": collector.get("stale_policy"),
        },
        "rate": payload.get("rate", {}),
        "note": ("Source: LLM Manager facility collector (Emporia). ACMS never "
                 "holds Emporia credentials. Stale data renders STALE + timestamp, never 0."),
    }


async def latest_power_summary(db: AsyncSession) -> dict | None:
    """Latest stored snapshot summary (no fetch)."""
    rec = (await db.scalars(
        select(PowerSnapshotRecord)
        .order_by(PowerSnapshotRecord.captured_at.desc()).limit(1))).first()
    if rec is None:
        return None
    try:
        payload = json.loads(rec.payload_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    return _summary_from(payload, rec)


async def usage_summary(db: AsyncSession, hours: int = 24) -> dict:
    """Cost/usage rollups from cost_attribution (§17 breakdowns).

    Unknown cost stays UNKNOWN — never coerced to zero (§17 + established
    REV2 semantics). Cloud = actual; local = estimated label enforced.
    """
    from sqlalchemy import func

    from .economics_models import CostAttributionRecord

    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    rows = (await db.execute(
        select(
            CostAttributionRecord.model_id,
            CostAttributionRecord.provider,
            func.sum(func.coalesce(CostAttributionRecord.input_tokens, 0)),
            func.sum(func.coalesce(CostAttributionRecord.output_tokens, 0)),
            func.sum(func.coalesce(CostAttributionRecord.cached_input_tokens, 0)),
            func.sum(func.coalesce(CostAttributionRecord.api_cost_usd, 0.0)),
            func.count(),
        )
        .where(CostAttributionRecord.created_at >= cutoff_dt)
        .group_by(CostAttributionRecord.model_id, CostAttributionRecord.provider)
        .order_by(func.sum(func.coalesce(CostAttributionRecord.api_cost_usd, 0.0)).desc())
    )).all()
    models = []
    total_cloud = 0.0
    total_in = total_out = total_cached = 0
    for model_id, provider, tin, tout, tcache, cost, n in rows:
        cloud = float(cost or 0.0)
        total_cloud += cloud
        total_in += int(tin or 0)
        total_out += int(tout or 0)
        total_cached += int(tcache or 0)
        models.append({
            "model_id": model_id or "UNKNOWN", "provider": provider or "UNKNOWN",
            "requests": int(n), "input_tokens": int(tin or 0),
            "output_tokens": int(tout or 0), "cached_input_tokens": int(tcache or 0),
            "cloud_cost_usd_actual": round(cloud, 6),
            "local_cost_usd": None,  # local attribution = estimated, rollup later (FW)
            "note": "local inference cost is ESTIMATED (never exceeds measured energy)",
        })
    return {
        "window_hours": hours,
        "total_cloud_cost_usd_actual": round(total_cloud, 6),
        "total_tokens": {"input": total_in, "output": total_out,
                         "cached_input": total_cached},
        "local_estimated_cost_usd": None,  # labeled estimated; not fabricated
        "models": models,
        "note": ("cloud cost is ACTUAL (LiteLLM spend); local inference cost "
                 "remains an ESTIMATE and is shown as such"),
    }