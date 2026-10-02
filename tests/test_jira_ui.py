"""Window-5 §13.5 UI tests — Check Jira now action + work-detail panels.

The UI action is a real POST mutation (admin-gated); GET never starts work.
Reuses the env/fixtures from test_jira_gate by importing the module-level
constants and re-declaring the env fixture (pytest fixtures don't import).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from acms.settings import get_settings
from tests.test_ui_auth import ADMIN_DN

client = TestClient(app, follow_redirects=False)

AI_ACCOUNT_ID = "712020:520fb263-ef0f-425c-a0be-14e9d258917e"


@pytest.fixture()
async def db():
    from acms.db import get_session

    async for session in get_session():
        yield session


@pytest.fixture(autouse=True)
def gate_env(monkeypatch):
    monkeypatch.setenv("ACMS_JIRA_AI_ACCOUNT_ID", AI_ACCOUNT_ID)
    monkeypatch.setenv("ACMS_JIRA_READY_STATUSES", "TO START")
    monkeypatch.delenv("ACMS_JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_EMAIL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("ACMS_JIRA_RECONCILE_ENABLED", "false")
    monkeypatch.setenv("ACMS_SESSION_SECRET", "ui-test-secret-0123456789abcdef")
    monkeypatch.setenv("ACMS_LDAP_URL", "ldaps://lldap.miam.home.arpa:636")
    monkeypatch.setenv("ACMS_LDAP_USER_BASE", "ou=people,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_LDAP_BIND_DN", "uid=acms,ou=people,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_LDAP_BIND_PASSWORD", "bind-secret")
    monkeypatch.setenv("ACMS_LDAP_GROUP_ADMIN", ADMIN_DN)
    monkeypatch.setenv("ACMS_LDAP_GROUP_WORKER", "cn=acms-worker,ou=groups,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_LDAP_GROUP_OBSERVER", "cn=acms-observer,ou=groups,dc=miam,dc=home,dc=arpa")
    monkeypatch.setenv("ACMS_ADMIN_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login_admin(monkeypatch):
    import types

    from tests.test_ui_auth import _install_fake_ldap3

    e = types.SimpleNamespace()
    e.entry_dn = "uid=jordan,ou=people,dc=example,dc=com"
    e.entry_attributes_as_dict = {"memberOf": [ADMIN_DN]}
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": "jordan", "password": "pw"})
    assert resp.status_code == 303
    return {"acms_session": resp.cookies["acms_session"]}


def test_check_now_get_405(gate_env):
    r = client.get("/ui/jira/check-now")
    assert r.status_code == 405


def test_check_now_requires_login(gate_env):
    r = client.post("/ui/jira/check-now")
    assert r.status_code == 303  # redirected to login


def test_check_now_coalesces_without_creds(gate_env, monkeypatch):
    """Mock-mode Jira (no creds): the run FAILS honestly — never fake success."""
    cookies = _login_admin(monkeypatch)
    r = client.post("/ui/jira/check-now", cookies=cookies, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "jira_run=" in loc and "jira_state=failed" in loc


def test_work_detail_shows_awaiting_jira(gate_env, monkeypatch):
    cookies = _login_admin(monkeypatch)
    r = client.post("/api/v1/work/items", headers={"Authorization": "Bearer test-token"},
                    json={"kind": "task", "title": "unlinked UI probe"})
    wid = r.json()["work_item_id"]
    page = client.get(f"/ui/work/{wid}", cookies=cookies)
    assert page.status_code == 200
    assert "Awaiting Jira linkage" in page.text
    assert "NOT executable" in page.text


def test_work_list_shows_jira_column_and_button(gate_env, monkeypatch):
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/work", cookies=cookies)
    assert page.status_code == 200
    assert "Check Jira now" in page.text
    assert "Jira" in page.text


def test_work_board_renders_columns(gate_env, monkeypatch):
    """§6.3/§12: kanban columns render; cards show real facts only.
    STEA-004 Phase C renamed columns (attention/terminal) and card link text
    (Work UID primary); Jira linkage stays visibly separate."""
    cookies = _login_admin(monkeypatch)
    r = client.post("/api/v1/work/items", headers={"Authorization": "Bearer test-token"},
                    json={"kind": "task", "title": "board card probe"})
    wid = r.json()["work_item_id"]
    page = client.get("/ui/work/board", cookies=cookies)
    assert page.status_code == 200
    assert "Planned" in page.text and "Running" in page.text \
        and "Human attention / blocked" in page.text and "Complete / failed" in page.text
    assert "board card probe" in page.text
    assert "no jira link" in page.text  # unlinked = visibly non-executable


async def test_usage_page_renders_honest_nulls(gate_env, monkeypatch):
    """§12.2: cost-per-accepted renders null with nothing accepted; unknown stays unknown."""
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/usage", cookies=cookies)
    assert page.status_code == 200
    assert "never a fabricated zero" in page.text
    assert "MERGED is never displayed as accepted" in page.text
    # filtered view also renders
    page2 = client.get("/ui/usage?since_days=30", cookies=cookies)
    assert page2.status_code == 200


def test_products_nav_and_new_form(gate_env, monkeypatch):
    """§8.1 entry point + §8.4 review screen contract."""
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/products", cookies=cookies)
    assert page.status_code == 200
    # STEA-004 §11: clearer buttons — "+ Create Product / Idea" + "+ Create Project"
    assert "+ Create Product / Idea" in page.text
    assert "+ Create Project" in page.text
    newp = client.get("/ui/products/new", cookies=cookies)
    assert newp.status_code == 200
    assert "Review idea" in newp.text


def test_product_preview_classifies_draft(gate_env, monkeypatch):
    """§8.4: the review screen shows the idea's own draft/unvalidated status."""
    cookies = _login_admin(monkeypatch)
    r = client.post("/ui/products/preview", cookies=cookies,
                    data={"markdown": _fixture_like_markdown(), "product_name": ""})
    assert r.status_code == 200
    assert "draft/unvalidated" in r.text
    assert "assumptions stay assumptions" in r.text
    assert "TO START" in r.text  # kickoff requirement displayed


def _fixture_like_markdown() -> str:
    return """---
title: "Probe Product Idea"
slug: "probe-product-idea"
status: "draft/unvalidated"
goodness_score: 77
validation_priority: "P1 - validate now"
validation_next_step: "Interview 10 operators"
---
# Probe Product Idea

## One-line summary

A probe product.

## Assumptions to validate

1. People want this.
"""


async def test_project_detail_404_human(gate_env, monkeypatch):
    """§14: unknown project → human 404 page, not raw JSON."""
    cookies = _login_admin(monkeypatch)
    r = client.get("/ui/projects/no-such-id", cookies=cookies)
    assert r.status_code == 404


async def test_project_detail_tabs(gate_env, monkeypatch, clean_db, db):
    from datetime import datetime, timezone
    from uuid import uuid4

    cookies = _login_admin(monkeypatch)
    now = datetime.now(timezone.utc)
    pid = str(uuid4())
    db.add(WorkItemRecord(work_item_id=pid, title="Probe Project", kind="project",
                          created_at=now, updated_at=now))
    await db.commit()
    page = client.get(f"/ui/projects/{pid}", cookies=cookies)
    assert page.status_code == 200
    assert "overview" in page.text
    page2 = client.get(f"/ui/projects/{pid}?tab=work", cookies=cookies)
    assert page2.status_code == 200 and "Work under this project" in page2.text


from acms.work_models import WorkItemRecord  # noqa: E402


async def test_work_detail_pr_panel_merged_not_accepted(gate_env, monkeypatch, clean_db, db):
    """§6.5: MERGED is never displayed as accepted."""
    from datetime import datetime, timezone

    from acms.economics_models import PrOutcomeRecord

    cookies = _login_admin(monkeypatch)
    r = client.post("/api/v1/work/items", headers={"Authorization": "Bearer test-token"},
                    json={"kind": "task", "title": "pr panel probe"})
    wid = r.json()["work_item_id"]
    now = datetime.now(timezone.utc)
    db.add(PrOutcomeRecord(outcome_id=str(__import__("uuid").uuid4()),
                            work_item_id=wid, pr_number=42, pr_url="https://github.com/x/pull/42",
                            repository="startupteams/x", branch="feat/x",
                            outcome_state="MERGED", created_at=now, updated_at=now))
    await db.commit()
    page = client.get(f"/ui/work/{wid}", cookies=cookies)
    assert page.status_code == 200
    assert "merged ≠ accepted" in page.text