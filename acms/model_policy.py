"""Model policy resolution (ADR-0015; STEA-004 plan §17-§20).

Precedence (highest wins):
    work item override > agent preference > project preference > system default

The system default row is migration-seeded: local-preferred /
qwen3.8-flash-next / cloud allowed. Resolution returns (policy dict, reason)
where reason ∈ {work_override, agent, project, system}.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .a2a_models import ModelPolicyRecord, ProjectRecord
from .work_models import WorkItemRecord

SCOPES = ("system", "agent", "project", "work_item")
INFERENCE_POLICIES = ("local-only", "local-preferred", "any")
REASON_BY_SCOPE = {
    "work_item": "work_override",
    "agent": "agent",
    "project": "project",
    "system": "system",
}


def _policy_dict(rec: ModelPolicyRecord) -> dict[str, Any]:
    return {
        "inference_policy": rec.inference_policy,
        "preferred_model": rec.preferred_model,
        "cloud_fallback": bool(rec.cloud_fallback),
        "scope": rec.scope,
        "policy_id": rec.policy_id,
    }


def _overrides_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _apply_overrides(base: dict[str, Any], overrides: dict[str, Any] | None,
                     reason: str) -> dict[str, Any]:
    """product.default_model_policy_json / project JSON overrides ride the
    project layer: they are merged into the resolved policy, never silently
    replace unknown keys."""
    if not overrides:
        return dict(base, resolution_reason=reason)
    merged = dict(base)
    for key in ("inference_policy", "preferred_model", "cloud_fallback"):
        if overrides.get(key) is not None:
            merged[key] = overrides[key]
    return dict(merged, resolution_reason=reason)


async def resolve_policy(db: AsyncSession, work_item_id: str | None,
                         agent_id: str | None) -> dict[str, Any]:
    """Full ADR-0015 precedence: work_item > agent > project > system.

    ``resolve_policy_for_work_item`` remains the work-item-only chain (used by
    the /model-policy/effective endpoint where no agent is bound yet); the
    dispatcher calls THIS with the target agent so the agent layer actually
    applies (hit live 2026-10-01: agent-scope rows were stored but never read
    at dispatch — the agent preference silently never won).
    """
    # 1. work-item override
    if work_item_id is not None:
        rec = (await db.scalars(
            select(ModelPolicyRecord).where(
                ModelPolicyRecord.scope == "work_item",
                ModelPolicyRecord.scope_id == work_item_id,
            )
        )).first()
        if rec is not None:
            return dict(_policy_dict(rec), resolution_reason="work_override")

    work = await db.get(WorkItemRecord, work_item_id) if work_item_id else None

    # 2. agent preference
    if agent_id is not None:
        rec = (await db.scalars(
            select(ModelPolicyRecord).where(
                ModelPolicyRecord.scope == "agent",
                ModelPolicyRecord.scope_id == agent_id,
            )
        )).first()
        if rec is not None:
            return dict(_policy_dict(rec), resolution_reason="agent")

    # 3/4. project + system chain (shared with the work-item resolver)
    project_policy_json = None
    if work is not None and work.project_id:
        rec = (await db.scalars(
            select(ModelPolicyRecord).where(
                ModelPolicyRecord.scope == "project",
                ModelPolicyRecord.scope_id == work.project_id,
            )
        )).first()
        if rec is not None:
            return dict(_policy_dict(rec), resolution_reason="project")
        project = await db.get(ProjectRecord, work.project_id)
        if project is not None:
            project_policy_json = project.default_model_policy_json

    rec = (await db.scalars(
        select(ModelPolicyRecord).where(ModelPolicyRecord.scope == "system")
    )).first()
    if rec is None:
        base = {"inference_policy": "local-preferred",
                "preferred_model": "qwen3.8-flash-next",
                "cloud_fallback": True, "scope": "system", "policy_id": None}
    else:
        base = _policy_dict(rec)
    return _apply_overrides(base, _overrides_json(project_policy_json), "system")


async def resolve_policy_for_work_item(db: AsyncSession, work_item_id: str) -> dict[str, Any]:
    """Resolve the effective model policy for a Work Item (ADR-0015)."""
    work = await db.get(WorkItemRecord, work_item_id)

    # 1. work-item override
    if work is not None:
        rec = (await db.scalars(
            select(ModelPolicyRecord).where(
                ModelPolicyRecord.scope == "work_item",
                ModelPolicyRecord.scope_id == work_item_id,
            )
        )).first()
        if rec is not None:
            return dict(_policy_dict(rec), resolution_reason="work_override")

    # 2. agent preference — resolved by the dispatcher (agent known there);
    #    here we only handle the project/system chain.
    # 3. project preference (project row JSON overrides + project scope row)
    project_policy_json = None
    if work is not None and work.project_id:
        rec = (await db.scalars(
            select(ModelPolicyRecord).where(
                ModelPolicyRecord.scope == "project",
                ModelPolicyRecord.scope_id == work.project_id,
            )
        )).first()
        if rec is not None:
            return dict(_policy_dict(rec), resolution_reason="project")
        project = await db.get(ProjectRecord, work.project_id)
        if project is not None:
            project_policy_json = project.default_model_policy_json

    # 4. system default
    rec = (await db.scalars(
        select(ModelPolicyRecord).where(ModelPolicyRecord.scope == "system")
    )).first()
    if rec is None:
        # fail-safe default mirroring the migration seed
        base = {"inference_policy": "local-preferred",
                "preferred_model": "qwen3.8-flash-next",
                "cloud_fallback": True, "scope": "system", "policy_id": None}
    else:
        base = _policy_dict(rec)
    return _apply_overrides(base, _overrides_json(project_policy_json), "system")


async def upsert_policy(db: AsyncSession, *, scope: str, scope_id: str | None,
                        inference_policy: str, preferred_model: str | None,
                        cloud_fallback: bool, updated_by: str | None = None) -> ModelPolicyRecord:
    if scope not in SCOPES:
        raise ValueError(f"unknown policy scope {scope!r}")
    if inference_policy not in INFERENCE_POLICIES:
        raise ValueError(f"unknown inference_policy {inference_policy!r}")
    if scope == "system":
        scope_id = None
    rec = (await db.scalars(
        select(ModelPolicyRecord).where(
            ModelPolicyRecord.scope == scope,
            ModelPolicyRecord.scope_id == scope_id,
        )
    )).first()
    if rec is None:
        rec = ModelPolicyRecord(
            policy_id=str(__import__("uuid").uuid4()), scope=scope, scope_id=scope_id,
        )
        db.add(rec)
    rec.inference_policy = inference_policy
    rec.preferred_model = preferred_model
    rec.cloud_fallback = cloud_fallback
    rec.updated_by = updated_by
    rec.updated_at = ModelPolicyRecord.now()
    await db.commit()
    await db.refresh(rec)
    return rec


async def get_policy(db: AsyncSession, scope: str, scope_id: str | None) -> ModelPolicyRecord | None:
    return (await db.scalars(
        select(ModelPolicyRecord).where(
            ModelPolicyRecord.scope == scope,
            ModelPolicyRecord.scope_id == scope_id if scope != "system" else ModelPolicyRecord.scope_id.is_(None),
        )
    )).first()


def build_fallback_chain(resolved: dict[str, Any], healthy_locals: list[str]) -> list[str]:
    """Local failover chain (plan §19): preferred first, then other healthy
    local models. Cloud is NOT in this chain — the dispatcher appends it only
    when policy allows and all locals are exhausted."""
    chain: list[str] = []
    preferred = resolved.get("preferred_model")
    if preferred:
        chain.append(preferred)
    for m in healthy_locals:
        if m not in chain:
            chain.append(m)
    return chain

def fetch_healthy_local_models() -> list[str]:
    """Healthy routable local models via Server Manager /api/v1/model-routes
    (ADR-0015: LLM Manager owns route truth; SM exposes it svc-token-authed).
    Returns logical model names (aliases excluded, cloud engines excluded).
    Fail-open: [] on any error (chain falls back to the preferred model only).
    """
    import json
    import urllib.request

    from .settings import get_settings

    s = get_settings()
    if not s.server_manager_base_url or not s.server_manager_token:
        return []
    try:
        req = urllib.request.Request(
            s.server_manager_base_url.rstrip("/") + "/api/v1/model-routes")
        req.add_header("Authorization", f"Bearer {s.server_manager_token}")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — fail-open
        return []
    out: list[str] = []
    for name, rows in (data.get("routes") or {}).items():
        for row in rows or []:
            if not row.get("routable") or row.get("is_alias"):
                continue
            if (row.get("health") or "").lower() != "healthy":
                continue
            engine = (row.get("engine") or "").lower()
            if engine in ("openrouter",) or "openrouter.ai" in (row.get("backend_url") or ""):
                continue  # cloud — never in the local chain
            out.append(name)
            break
    return sorted(set(out))
