"""Agent Bridge for Hermes (combined slice 3+4 plan §15; ADR-0010; REQ-031/032).

Translates ACMS bridge capabilities (status / send_work / steer / pause /
resume / interrupt / cancel / request_handoff / set_session_title) to the
**live-tested** Hermes api-server surface (docs/HARNESS_CONTROL_MAPPING.md).

Capability honesty (plan §12/§13): `pause`/`resume` are NOT supported by
Hermes 0.17.0 (verified live: 404) and are advertised unsupported — never
faked with interrupt.

Bridge identity/credentials come from ``acms_bridge_targets`` config (env:
``ACMS_BRIDGE_TARGETS`` = JSON list of {agent_id, base_url, api_key}); the
bridge runs where Hermes runs (VM906) or anywhere with network access to the
private gateway — ACMS core never imports this module in tests that don't
exercise it (plan §15: Hermes internals stay out of ACMS domain logic).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .telemetry_models import HeartbeatPayload, HeartbeatIdentity, HeartbeatSession

# Capabilities proven live against Hermes 0.17.0 (2026-09-26/27; see
# docs/HARNESS_CONTROL_MAPPING.md for the test log).
HERMES_SUPPORTED_CAPABILITIES = (
    "status", "send_work", "steer", "interrupt", "cancel",
    "request_handoff", "set_session_title",
)


class BridgeError(Exception):
    """Bridge transport/protocol failure (maps to reconciliation failure)."""


def _post_json(url: str, key: str, body: dict | None = None, method: str = "POST",
               headers: dict | None = None, timeout: int = 20) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise BridgeError(f"{e.code} {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BridgeError(str(e)) from None


def _get_json(base_url: str, key: str, path: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(f"{base_url}{path}")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise BridgeError(f"{e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BridgeError(str(e)) from None


@dataclass
class BridgeTarget:
    agent_id: str
    base_url: str
    api_key: str
    harness: str = "hermes"


def load_targets() -> list[BridgeTarget]:
    from .settings import get_settings

    raw = get_settings().bridge_targets_json
    targets = []
    for item in json.loads(raw) if raw else []:
        targets.append(BridgeTarget(
            agent_id=item["agent_id"],
            base_url=item["base_url"].rstrip("/"),
            api_key=item["api_key"],
            harness=item.get("harness", "hermes"),
        ))
    return targets


def get_bridge_for_agent(agent_id: str):
    """Factory used by the reconciliation scheduler (lazy import kept here)."""
    for t in load_targets():
        if t.agent_id == agent_id:
            return HermesBridge(t)
    raise BridgeError(f"no bridge target configured for agent {agent_id}")


class HermesBridge:
    """Tested Hermes api-server mapping. Every method here corresponds to a
    row in docs/HARNESS_CONTROL_MAPPING.md."""

    def __init__(self, target: BridgeTarget):
        self._t = target

    # ---------------------------------------------------------------- status

    def fetch_status(self) -> HeartbeatPayload:
        """Full status snapshot via the tested mapping (reconciliation path).

        Raises BridgeError when unreachable — the scheduler maps that to
        UNREACHABLE per ADR-0010."""
        sessions = self._get("/api/sessions")
        rows = sessions.get("data") or sessions.get("sessions") or []
        if not rows:
            raise BridgeError("no sessions reported")
        # Most recently active session = the agent's current work context.
        rows.sort(key=lambda s: s.get("last_active") or 0, reverse=True)
        cur = rows[0]
        sess = self._get(f"/api/sessions/{cur['id']}").get("session", {})
        return HeartbeatPayload(
            schema_version="acms-heartbeat-v1",
            identity=HeartbeatIdentity(
                acms_agent_id=self._t.agent_id,
                bridge_id="hermes-api-server",
                harness="hermes",
            ),
            session=HeartbeatSession(
                session_id=str(sess.get("id") or cur.get("id") or ""),
                title=sess.get("title"),
                agent_running=False,  # sessions API has no live running flag; reconciliation is contact-only
            ),
            platforms={"connected": ["api_server"]},
        )

    # ---------------------------------------------------------------- controls

    def send_work(self, *, work_key: str, assignment_key: str, instruction: str,
                  session_id: str | None = None) -> dict:
        """Dispatch work. Uses run submission (new session) or session-bound
        chat (existing session); includes ACMS keys in metadata per REQ-052."""
        meta = {"acms_work_key": work_key, "acms_assignment_key": assignment_key,
                "source": "acms"}
        if session_id:
            return _post_json(f"{self._t.base_url}/api/sessions/{session_id}/chat",
                              self._t.api_key,
                              {"message": instruction},
                              headers={"X-Hermes-Session-Id": session_id})
        return _post_json(f"{self._t.base_url}/v1/runs", self._t.api_key, {
            "input": [{"role": "user", "content": instruction}],
            "metadata": meta,
            "title": f"[{work_key}] ACMS dispatched work",
        })

    def steer(self, session_id: str, message: str) -> dict:
        return _post_json(f"{self._t.base_url}/api/sessions/{session_id}/chat",
                          self._t.api_key,
                          {"message": message},
                          headers={"X-Hermes-Session-Id": session_id})

    def interrupt(self, run_id: str) -> dict:
        return _post_json(f"{self._t.base_url}/v1/runs/{run_id}/stop", self._t.api_key)

    # `cancel` uses the same tested terminal endpoint as interrupt (A2A-task
    # level); the Hermes session survives — new execution task to continue.
    cancel = interrupt

    def set_session_title(self, session_id: str, title: str) -> dict:
        return _post_json(f"{self._t.base_url}/api/sessions/{session_id}",
                          self._t.api_key, {"title": title}, method="PATCH")

    def request_handoff(self, session_id: str, work_key: str) -> dict:
        """Plan §10: structured Markdown handoff request (via send_work)."""
        instruction = (
            f"Produce a structured Markdown handoff for [{work_key}]: what you "
            "believe you are doing, current goal, actions completed, "
            "artifacts/files changed, blockers, and next intended action. "
            "Use 'Observed:' vs 'Interpretation:' conventions."
        )
        return self.send_work(work_key=work_key, assignment_key="",
                              instruction=instruction, session_id=session_id)

    # ---------------------------------------------------------------- helpers

    def _get(self, path: str) -> dict:
        return _get_json(self._t.base_url, self._t.api_key, path)
