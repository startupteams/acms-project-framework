"""REV2 plan §7/§8: session-rotation policy + reviewer-context package tests.

Rotation (§7): thresholds are configurable + experimental; no auto-rotate on
fixed thresholds; SESSION_ROTATION_REQUIRED is the harness-independent contract.
Reviewer context (§8): default context packages/reconstructed packages carry
references, NEVER raw transcripts; transcript access is explicit-only.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


# ------------------------------------------------------------------- §7 policy


def test_advisory_thresholds_configurable(monkeypatch):
    from acms.memory_api import (ADVISORY_CHECKPOINT, ADVISORY_GREEN,
                                 ADVISORY_ROTATE, compute_advisory)

    # defaults (50/70/85)
    assert compute_advisory(55.0, 100) == "MONITOR"
    assert compute_advisory(72.0, 100) == ADVISORY_CHECKPOINT
    assert compute_advisory(90.0, 100) == ADVISORY_ROTATE
    # experimental override: threshold shapes are policy, not law
    assert compute_advisory(75.0, 100, monitor_pct=80, checkpoint_pct=90, rotate_pct=95) == ADVISORY_GREEN
    assert compute_advisory(90.0, 100, monitor_pct=80, checkpoint_pct=90, rotate_pct=95) == ADVISORY_CHECKPOINT
    assert compute_advisory(96.0, 100, monitor_pct=80, checkpoint_pct=90, rotate_pct=95) == ADVISORY_ROTATE


def test_settings_rotation_policy_fields():
    from acms.settings import get_settings

    s = get_settings()
    assert s.session_advisory_monitor_percent == 50
    assert s.session_advisory_checkpoint_percent == 70
    assert s.session_advisory_rotate_percent == 85
    # auto-rotation is feature-flagged OFF: never rotate on threshold alone
    assert s.session_rotation_auto_enabled is False


def test_rotation_required_event_emitted(client):
    agent_id = "00000000-0000-0000-0000-00000000bbb1"
    s = client.post("/api/v1/memory/sessions", headers=AUTH,
                    json={"agent_id": agent_id}).json()
    r = client.post(f"/api/v1/memory/sessions/{s['session_id']}/rotation-required",
                    headers=AUTH, json={"reason": "harness lacks create-session"})
    assert r.status_code == 201
    assert r.json()["event_type"] == "SESSION_ROTATION_REQUIRED"
    events = client.get("/api/v1/fleet/events?event_type=SESSION_ROTATION_REQUIRED",
                        headers=AUTH).json()
    assert events and "orchestrator" in events[0]["summary"]


def test_rotation_required_unknown_session_404(client):
    r = client.post("/api/v1/memory/sessions/00000000-0000-0000-0000-00000000zzzz/rotation-required",
                    headers=AUTH, json={})
    assert r.status_code == 404


# --------------------------------------------------------------- §8 reviewer ctx


def _work_with_session(client) -> tuple[str, str]:
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "reviewer ctx"}).json()["work_item_id"]
    s = client.post("/api/v1/memory/sessions", headers=AUTH,
                    json={"agent_id": "00000000-0000-0000-0000-00000000bbb1",
                          "work_item_id": w}).json()
    client.post("/api/v1/memory/checkpoints", headers=AUTH, json={
        "session_id": s["session_id"], "work_item_id": w, "reason": "rotate",
        "summary": "Slice done, tests green.",
        "work_id": w, "status": "done", "git_state": "main @ abc",
        "tests": "5/5", "deployment": "none", "decisions": "x", "debt": "y",
        "remaining_work": "z", "next_action": "merge", "artifacts": ["git://abc"]})
    return w, s["session_id"]


def test_reconstructed_package_contains_no_transcript(client):
    """Default rotation package: work facts + checkpoint refs — never transcripts."""
    w, s_id = _work_with_session(client)
    pkg = client.post(f"/api/v1/memory/work/{w}/rotate", headers=AUTH,
                      json={"prior_session_id": s_id}).json()
    raw = str(pkg)
    # the word may appear only in the explicit "no transcripts" note — a
    # transcript SOURCE type must never appear
    types = [s["type"] for s in pkg["selected"]]
    assert "raw_transcript" not in types
    assert "transcript" not in types
    assert "work_item" in types and "checkpoint" in types
    assert "no transcripts" in pkg["notes"]
    # explicit policy marker for reviewer-context selection
    assert pkg["policy_version"].startswith(("rotate", "advisory", "reviewer"))


def test_context_package_accepts_reviewer_policy_without_transcript(client):
    """Reviewer packages built via API select references; the API never injects
    a transcript body — callers pass references, and the package records policy."""
    w, _ = _work_with_session(client)
    pkg = client.post("/api/v1/memory/context-packages", headers=AUTH, json={
        "work_item_id": w,
        "policy_version": "reviewer-default-v1",
        "selected": [
            {"type": "requirement", "id": "ACMS-REQ-059"},
            {"type": "artifact", "id": "diff://PR-25"},
            {"type": "artifact", "id": "tests://pytest/105-passed"},
            {"type": "reference", "id": "handoff://latest", "note": "on demand"},
        ]}).json()
    types = [s["type"] for s in pkg["selected"]]
    assert types == ["requirement", "artifact", "artifact", "reference"]
    assert "raw_transcript" not in types
    assert pkg["policy_version"] == "reviewer-default-v1"
