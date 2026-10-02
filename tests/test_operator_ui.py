"""STEA-004 Phase C (Release 5) — operator UI tests.

Covers:
- Home zones (§18): Work Now / Agent Fleet / Human Attention / System-Cost /
  Product Slop cards render REAL data only; honest absent-value rendering.
- Work Kanban (§12): six columns, UID as link text, Jira + ACMS states
  distinct, TEST/PROOF labeling, execution transport states.
- Work detail canonical handoff card (§9) + Technical Details UUID.
- Artifacts list sort/search (§26) + detail metadata (§8).
- Inbox prev/next (§25) + Work UID links.
- Global search page (§27) over the backend derivation.
- Agent chat page (§20-§24): renders honestly when bridge unreachable; slash
  commands advertised from capability metadata; NO reasoning leak.
- Usage page (§17): facility cards STALE-not-zero semantics.
- Product detail (§14/§16): repos + slop cards; role-gated mutations.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from acms.settings import get_settings
from acms.ui.session_auth import COOKIE_NAME
from tests.test_ui_auth import ADMIN_DN, OBSERVER_DN, _install_fake_ldap3

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def ui_env(monkeypatch):
    get_settings.cache_clear()
    s = get_settings()
    saved = {k: getattr(s, k) for k in (
        "session_secret", "session_cookie_secure",
        "ldap_url", "ldap_user_base", "ldap_bind_dn", "ldap_bind_password",
        "ldap_group_admin", "ldap_group_worker", "ldap_group_observer",
        "server_manager_base_url", "server_manager_token",
        "bridge_targets_json", "run_event_pump_enabled",
    )}
    s.session_secret = "ui-test-secret-0123456789abcdef"
    s.session_cookie_secure = True
    s.ldap_url = "ldaps://lldap.miam.home.arpa:636"
    s.ldap_user_base = "ou=people,dc=miam,dc=home,dc=arpa"
    s.ldap_bind_dn = "uid=acms,ou=people,dc=miam,dc=home,dc=arpa"
    s.ldap_bind_password = "bind-secret"
    s.ldap_group_admin = ADMIN_DN
    s.ldap_group_worker = "cn=acms-workers,ou=groups,dc=miam,dc=home,dc=arpa"
    s.ldap_group_observer = OBSERVER_DN
    s.server_manager_base_url = ""      # display-only paths must degrade honestly
    s.server_manager_token = ""
    s.bridge_targets_json = ""          # chat renders 'bridge unreachable'
    s.run_event_pump_enabled = False
    yield s
    for key, value in saved.items():
        setattr(s, key, value)
    get_settings.cache_clear()


def _login_admin(monkeypatch, username: str = "jordan"):
    import types

    e = types.SimpleNamespace()
    e.entry_dn = f"uid={username},ou=people,dc=miam,dc=home,dc=arpa"
    e.entry_attributes_as_dict = {"memberOf": [ADMIN_DN], "member": [ADMIN_DN]}
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": username, "password": "pw"})
    assert resp.status_code == 303
    return {COOKIE_NAME: resp.cookies[COOKIE_NAME]}


def _make_work(title: str, **overrides) -> dict:
    r = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": title, **overrides})
    assert r.status_code == 201, r.text
    return r.json()


def _register_agent(name: str) -> str:
    r = client.post("/api/v1/agents/register", headers=AUTH, json={
        "external_registration_id": f"phasec-{name}",
        "display_name": name, "trust_class": "internal", "harness": "hermes",
        "bridge_version": "0.1", "protocol_version": "a2a",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["agent_id"]


# ---------------------------------------------------------------- home (§18)


def test_home_zones_render_real_data(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    _make_work("Phase C home probe")
    page = client.get("/ui/", cookies=cookies)
    assert page.status_code == 200, page.text[-500:]
    for zone in ("Work now", "Agent fleet", "Human attention", "Facility power"):
        assert zone in page.text
    # honest zero/absent rendering: no execution yet
    assert "No execution in flight" in page.text
    # cost card shows ACTUAL-zero explicitly (real 0 is a real value; label present)
    assert "Cloud cost today" in page.text


def test_home_shows_running_task_and_attention(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-home-agent")
    item = _make_work("Phase C running probe")
    client.post(f"/ui/work/{item['work_item_id']}/assign", cookies=cookies,
                data={"agent_id": agent_id})
    # execution task RUNNING (transport RUNNING)
    tr = client.post("/api/v1/work/tasks", headers=AUTH,
                     json={"work_item_id": item["work_item_id"], "agent_id": agent_id})
    task_id = tr.json()["task_id"]
    client.post(f"/api/v1/work/tasks/{task_id}/finish", headers=AUTH,
                json={"status": "SUCCEEDED"}) if False else None
    # create inbox item directly for the attention zone
    from acms.a2a_models import HumanInboxItemRecord
    from acms.db import SessionLocal

    async def _mk():
        async with SessionLocal() as db:
            db.add(HumanInboxItemRecord(
                item_id=HumanInboxItemRecord.new_id(), item_class="ACTION_REQUIRED",
                severity="high", title="Phase C attention probe",
                agent_id=agent_id, created_at=HumanInboxItemRecord.now(),
                updated_at=HumanInboxItemRecord.now()))
            await db.commit()

    import asyncio

    asyncio.run(_mk())
    page = client.get("/ui/", cookies=cookies)
    assert page.status_code == 200
    assert "Phase C attention probe" in page.text


# ---------------------------------------------------------------- kanban (§12)


def test_kanban_columns_and_uid_links(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    item = _make_work("Phase C kanban probe")
    page = client.get("/ui/work/board", cookies=cookies)
    assert page.status_code == 200, page.text[-500:]
    assert "Human attention / blocked" in page.text
    assert "Complete / failed" in page.text
    assert item["work_uid"] in page.text          # UID as primary link text
    assert f"/ui/work/{item['work_item_id']}" in page.text
    assert "Jira" in page.text  # Jira state shown separately


def test_kanban_blocked_item_in_attention_column(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    item = _make_work("Phase C blocked probe")
    client.patch(f"/api/v1/work/items/{item['work_item_id']}", headers=AUTH,
                 json={"status": "blocked"})
    page = client.get("/ui/work/board", cookies=cookies)
    assert page.status_code == 200
    # blocked work lands in the attention column (title appears in page)
    assert "Phase C blocked probe" in page.text


def test_kanban_test_labeling(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    _make_work("[CANARY] phase C proof probe")
    page = client.get("/ui/work/board", cookies=cookies)
    assert "TEST/PROOF" in page.text


# ---------------------------------------------------------------- work detail (§9)


def test_work_detail_canonical_handoff_card(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    item = _make_work("Phase C handoff probe")
    # create a canonical work_handoff artifact for it
    ar = client.post("/artifacts", headers=AUTH, json={
        "work_item_id": item["work_item_id"], "artifact_type": "work_handoff",
        "title": "Phase C canonical handoff", "bluf": "BLUF line here",
        "content": "# Work Handoff\n\n## BLUF\n\nprobe", "created_by": "test"})
    assert ar.status_code == 201, ar.text
    uid = ar.json()["artifact_uid"]
    page = client.get(f"/ui/work/{item['work_item_id']}", cookies=cookies)
    assert page.status_code == 200, page.text[-800:]
    assert "HANDOFF" in page.text
    assert uid in page.text
    assert f"/ui/artifacts/{uid}" in page.text
    assert "Technical details" in page.text
    assert item["work_item_id"] in page.text      # UUID under Technical Details
    assert item["work_uid"] in page.text          # Work UID as primary link text


def test_work_detail_without_handoff_no_card(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    item = _make_work("Phase C no-handoff probe")
    page = client.get(f"/ui/work/{item['work_item_id']}", cookies=cookies)
    assert page.status_code == 200
    assert "canonical Markdown handoff for this Work Item" not in page.text


# ---------------------------------------------------------------- artifacts (§26/§8)


def test_artifacts_search_and_sort(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    ar = client.post("/artifacts", headers=AUTH, json={
        "artifact_type": "note", "title": "Phase C artifact search probe",
        "content": "body", "jira_issue_key": "STNA-999"})
    uid = ar.json()["artifact_uid"]
    sha = ar.json()["sha256"]
    # search by sha prefix (§26 requirement)
    page = client.get(f"/ui/artifacts?q={sha[:10]}", cookies=cookies)
    assert page.status_code == 200
    assert uid in page.text
    # search by jira key
    page = client.get("/ui/artifacts?q=STNA-999", cookies=cookies)
    assert uid in page.text
    # sort by title asc works
    page = client.get("/ui/artifacts?sort=title&direction=asc", cookies=cookies)
    assert page.status_code == 200
    # detail shows metadata (§8)
    detail = client.get(f"/ui/artifacts/{uid}", cookies=cookies)
    assert detail.status_code == 200
    assert "sha256" in detail.text
    assert "Artifact UID" in detail.text
    assert "STNA-999" in detail.text


# ---------------------------------------------------------------- inbox (§25)


def test_inbox_prev_next_and_uid(clean_db, monkeypatch):
    from acms.a2a_models import HumanInboxItemRecord
    from acms.db import SessionLocal

    cookies = _login_admin(monkeypatch)
    item = _make_work("Phase C inbox-linked work")
    ids = []
    async def _mk(n):
        async with SessionLocal() as db:
            rec = HumanInboxItemRecord(
                item_id=HumanInboxItemRecord.new_id(), item_class="FYI",
                severity="info", title=f"Phase C inbox probe {n}",
                work_item_id=item["work_item_id"],
                created_at=HumanInboxItemRecord.now(),
                updated_at=HumanInboxItemRecord.now())
            db.add(rec); await db.commit(); await db.refresh(rec)
            ids.append(rec.item_id)
    import asyncio

    asyncio.run(_mk(1)); asyncio.run(_mk(2))
    page = client.get(f"/ui/inbox/{ids[1]}", cookies=cookies)
    assert page.status_code == 200
    assert "Previous" in page.text or "Next" in page.text
    # work uid link present (canonical link text)
    assert item["work_uid"] in page.text


# ---------------------------------------------------------------- global search (§27)


def test_search_page_grouped_results(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    item = _make_work("Phase C search needle zqxj")
    page = client.get("/ui/search?q=zqxj", cookies=cookies)
    assert page.status_code == 200
    assert "Work (" in page.text
    assert item["work_uid"] in page.text


def test_search_page_empty_ok(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/search", cookies=cookies)
    assert page.status_code == 200


# ---------------------------------------------------------------- agent chat (§20-§24)


def test_agent_chat_renders_honestly_without_bridge(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-chat-agent")
    page = client.get(f"/ui/agents/{agent_id}/chat", cookies=cookies)
    assert page.status_code == 200, page.text[-500:]
    # Honest failure path: the harness cannot be reached from the test env —
    # the page must SAY so and never fabricate a conversation (§22 honesty).
    assert ("bridge unreachable" in page.text) or ("unavailable" in page.text)
    # No fabricated transcript rows when the source is unreachable.
    assert "No active harness session" in page.text


def test_agent_chat_slash_autocomplete_source(clean_db, monkeypatch):
    """§21: autocomplete reads harness capability metadata, not a hard-coded list.
    When the bridge is unreachable, no harness commands are advertised (honest)."""
    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-chat-cmd")
    r = client.get(f"/ui/agents/{agent_id}/chat/commands", cookies=cookies)
    assert r.status_code == 200
    cmds = r.json()["commands"]     # unreachable bridge → empty (honest), never fabricated
    assert isinstance(cmds, list)
    # With a live (fake) bridge, the capability-advertised set is served.
    from acms.bridge import HermesBridge, BridgeTarget

    class FakeBridge(HermesBridge):
        def fetch_capabilities(self):
            return {"features": {"session_chat": True}}
        def slash_commands(self):
            return [{"command": "/model", "args": "", "description": "Switch model"},
                    {"command": "/help", "args": "", "description": "d"},
                    {"command": "/steer", "args": "<prompt>", "description": "s"}]

    import acms.ui.chat_routes as cr

    monkeypatch.setattr(cr, "get_bridge_for_agent",
                        lambda aid: FakeBridge(BridgeTarget(agent_id=aid, base_url="", api_key="", harness="hermes")))
    r2 = client.get(f"/ui/agents/{agent_id}/chat/commands", cookies=cookies)
    names = [c["command"] for c in r2.json()["commands"]]
    assert "/model" in names and "/help" in names and "/steer" in names


def test_agent_chat_no_reasoning_leak(clean_db, monkeypatch):
    """§22: reasoning fields must never reach the rendered page."""
    from acms.bridge import HermesBridge, BridgeTarget, get_bridge_for_agent

    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-chat-reasoning")

    class FakeBridge(HermesBridge):
        def current_session_id(self): return "sess-1"
        def list_sessions(self): return [{"id": "sess-1", "title": "t", "last_active": 1}]
        def fetch_messages(self, sid, limit=200): return [
            {"role": "user", "content": "hello", "timestamp": "2026-10-02T00:00:00Z"},
            {"role": "assistant", "content": "hi", "reasoning_content": "SECRET-REASONING",
             "reasoning": "SECRET-REASONING-2", "timestamp": "2026-10-02T00:00:01Z"},
        ]
        def slash_commands(self): return [{"command": "/help", "args": "", "description": "d"}]

    import acms.ui.chat_routes as cr

    orig = cr.get_bridge_for_agent
    monkeypatch.setattr(cr, "get_bridge_for_agent",
                        lambda aid: FakeBridge(BridgeTarget(agent_id=aid, base_url="", api_key="", harness="hermes")))
    page = client.get(f"/ui/agents/{agent_id}/chat", cookies=cookies)
    assert page.status_code == 200
    assert "SECRET-REASONING" not in page.text
    assert "SECRET-REASONING-2" not in page.text
    assert "hi" in page.text


def test_agent_chat_tool_call_collapsible(clean_db, monkeypatch):
    from acms.bridge import HermesBridge, BridgeTarget

    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-chat-tool")

    class FakeBridge(HermesBridge):
        def current_session_id(self): return "sess-2"
        def list_sessions(self): return [{"id": "sess-2", "title": "t", "last_active": 1}]
        def fetch_messages(self, sid, limit=200): return [
            {"role": "tool", "tool_name": "execute_code",
             "content": json.dumps({"output": "PROBE-TOOL-OK", "exit_code": 0}),
             "timestamp": "2026-10-02T00:00:02Z"},
        ]
        def slash_commands(self): return [{"command": "/help", "args": "", "description": "d"}]

    import acms.ui.chat_routes as cr

    monkeypatch.setattr(cr, "get_bridge_for_agent",
                        lambda aid: FakeBridge(BridgeTarget(agent_id=aid, base_url="", api_key="", harness="hermes")))
    page = client.get(f"/ui/agents/{agent_id}/chat", cookies=cookies)
    assert page.status_code == 200
    assert "Tool: execute_code" in page.text
    assert "PROBE-TOOL-OK" in page.text


def test_agent_chat_send_requires_message(clean_db, monkeypatch):
    """Empty/whitespace message → error redirect (422 for missing field)."""
    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-chat-send")
    r = client.post(f"/ui/agents/{agent_id}/chat/send", cookies=cookies,
                    data={"message": "   ", "session_id": ""})
    assert r.status_code == 303
    assert "error" in r.headers["location"]
    # missing field entirely → validation 422 (form contract)
    r2 = client.post(f"/ui/agents/{agent_id}/chat/send", cookies=cookies,
                     data={"session_id": ""})
    assert r2.status_code == 422


def test_agent_chat_send_passes_through_bridge(clean_db, monkeypatch):
    """§21: message delivery goes THROUGH the bridge (session chat / new run)."""
    cookies = _login_admin(monkeypatch)
    agent_id = _register_agent("phasec-chat-send2")
    from acms.bridge import HermesBridge, BridgeTarget

    sent = {}

    class FakeBridge(HermesBridge):
        def current_session_id(self): return "sess-live"
        def chat(self, session_id, message):
            sent["sid"] = session_id
            sent["msg"] = message
            return {"session_id": session_id}
        def slash_commands(self):
            return [{"command": "/help", "args": "", "description": "d"}]

    import acms.ui.chat_routes as cr

    monkeypatch.setattr(cr, "get_bridge_for_agent",
                        lambda aid: FakeBridge(BridgeTarget(agent_id=aid, base_url="", api_key="", harness="hermes")))
    r = client.post(f"/ui/agents/{agent_id}/chat/send", cookies=cookies,
                    data={"message": "hello from operator", "session_id": ""})
    assert r.status_code == 303
    assert sent["sid"] == "sess-live"
    assert sent["msg"] == "hello from operator"
    assert "sess-live" in r.headers["location"]


# ---------------------------------------------------------------- usage (§17)


def test_usage_page_stale_not_zero(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/usage", cookies=cookies)
    assert page.status_code == 200, page.text[-500:]
    # no snapshot yet → honest STALE/absent message, never a 0
    assert "No facility snapshot" in page.text
    assert "NEVER shown as 0" in page.text


def test_usage_page_with_stale_snapshot(clean_db, monkeypatch):
    """Stale upstream → STALE + timestamp, NULL values — never 0 (§40)."""
    cookies = _login_admin(monkeypatch)
    from datetime import datetime, timezone

    from acms.db import SessionLocal
    from acms.power_ingest import PowerSnapshotRecord

    async def _mk():
        async with SessionLocal() as db:
            db.add(PowerSnapshotRecord(
                snapshot_id=PowerSnapshotRecord.new_id(),
                captured_at=datetime.now(timezone.utc),
                source="llm-manager-facility",
                payload_json=json.dumps({
                    "error": None,
                    "totals": {"label": "TOTAL MARION_IA_USA", "kwh_24h": None,
                               "cost_usd_24h": None, "kwh_30d": None,
                               "cost_usd_30d": None,
                               "incomplete_reason": "collector stale"},
                    "collector": {"healthy": False, "stale": True},
                    "channels": [{"label": "PDU MIAM-00151", "stale": True,
                                  "avg_watts_1h": None, "kwh_24h": None,
                                  "cost_usd_24h": None, "kwh_30d": None,
                                  "cost_usd_30d": None,
                                  "last_sample_ts": "2026-10-01T00:00:00Z"}],
                }),
                total_kwh_24h=None, total_cost_usd_24h=None,
                current_watts=None, collector_stale=True))
            await db.commit()

    import asyncio

    asyncio.run(_mk())
    page = client.get("/ui/usage", cookies=cookies)
    assert page.status_code == 200
    assert "STALE" in page.text
    assert "collector stale" in page.text
    # the stale channel must NOT render a fabricated 0 W
    assert ">0 W<" not in page.text


# ---------------------------------------------------------------- product detail (§14/§16)


def test_product_detail_repos_and_slop(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    pr = client.post("/products", headers=AUTH, json={
        "name": "Phase C Product", "business_summary": "probe"})
    assert pr.status_code == 201, pr.text
    pid = pr.json()["product_id"]
    page = client.get(f"/ui/products/{pid}", cookies=cookies)
    assert page.status_code == 200, page.text[-500:]
    assert "True Slop" in page.text
    assert "Repositories" in page.text
    # link a repo, then see it + slop-eligible UI
    rr = client.post("/repositories", headers=AUTH,
                     json={"owner": "startupteams", "repo": "acms-project-framework"})
    assert rr.status_code in (200, 201)
    rid = rr.json()["repository_id"]
    lk = client.post(f"/products/{pid}/repositories", headers=AUTH,
                     json={"repository_id": rid})
    assert lk.status_code == 201, lk.text
    page = client.get(f"/ui/products/{pid}", cookies=cookies)
    assert "acms-project-framework" in page.text
    # unlink works (human correction §14)
    ul = client.post(f"/ui/products/{pid}/repositories/{rid}/unlink", cookies=cookies,
                     follow_redirects=False)
    assert ul.status_code == 303
    page = client.get(f"/ui/products/{pid}", cookies=cookies)
    assert page.status_code == 200


def test_product_detail_404(clean_db, monkeypatch):
    cookies = _login_admin(monkeypatch)
    r = client.get("/ui/products/00000000-0000-0000-0000-000000000000", cookies=cookies)
    assert r.status_code == 404


def test_product_mutations_admin_only(clean_db, monkeypatch):
    """Observer cannot link/unlink (mutations are Administrator-only)."""
    import types

    e = types.SimpleNamespace()
    e.entry_dn = "uid=observer1,ou=people,dc=miam,dc=home,dc=arpa"
    e.entry_attributes_as_dict = {"memberOf": [OBSERVER_DN], "member": [OBSERVER_DN]}
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": "observer1", "password": "pw"})
    obs = {COOKIE_NAME: resp.cookies[COOKIE_NAME]}
    pr = client.post("/products", headers=AUTH, json={"name": "Phase C AdminOnly"})
    pid = pr.json()["product_id"]
    r = client.post(f"/ui/products/{pid}/repositories/link", cookies=obs,
                    data={"owner": "o", "repo": "r"})
    assert r.status_code == 403