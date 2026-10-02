"""PDU + Emporia power domain adapter (plan §27) — Server Manager is the authority.

The gateway NEVER talks to PDUs directly and holds NO Emporia credentials; every
read here is a thin REST call against Server Manager (:8300, svc-acms bearer),
which already owns the PDU Manager contract (real backend, protected-outlet
invariants) and the facility-power rollup.

Surface (plan §27):

Resources (READ; no ordinary-worker power mutation exists):
  pdu.list              — PDUs known to the PDU Manager (identity + health)
  pdu.status            — per-PDU status
  pdu.outlet_status     — per-outlet states for one PDU (query: pdu key)
  pdu.protection_state  — protected outlets + controllability (policy view)
  power.facility.current — per-channel + TOTAL MARION_IA_USA current draw
  power.facility.history — 24h/30d kWh + cost rollups per channel
  power.pdu.current     — the three PDU channels only
  power.cooling.current — the 36k mini-split channel
  power.cost.summary    — cost components (rate + 24h/30d $)

Hard limits (plan §16/§27/§43):
- NO power mutation for ANY worker (pdu.outlet.power_off = DESTRUCTIVE, denied).
- Executive pdu.request_reboot rides the SM action-plan DRY-RUN + approval flow
  (SENSITIVE_WRITE, approval-gated; actuation stays env-gated server-side).
- Emporia credentials are never exposed or proxied.
"""
from __future__ import annotations

from .llm_adapter import ServerManagerClient  # re-use the thin SM client


class PowerClient(ServerManagerClient):
    """Same transport as the LLM/SM client — different endpoint surface."""

    # ------------------------------------------------------------------ PDU
    def pdu_list(self) -> dict:
        status, body = self.request("GET", "/api/v1/pdu/health")
        if status != 200:
            raise DomainUnavailableError(f"pdu health failed (HTTP {status})")
        return body

    def pdu_capabilities(self) -> dict:
        status, body = self.request("GET", "/api/v1/pdu/capabilities")
        if status != 200:
            raise DomainUnavailableError(f"pdu capabilities failed (HTTP {status})")
        return body

    def pdu_assets(self) -> dict:
        status, body = self.request("GET", "/api/v1/pdu/assets")
        if status != 200:
            raise DomainUnavailableError(f"pdu assets failed (HTTP {status})")
        return body

    def pdu_outlet_status(self, pdu_key: str, outlet: int) -> dict:
        status, body = self.request(
            "GET", f"/api/v1/pdus/{pdu_key}/outlets/{int(outlet)}")
        if status == 404:
            raise NotFoundError(f"unknown pdu/outlet: {pdu_key}/{outlet}")
        if status != 200:
            raise DomainUnavailableError(f"pdu outlet status failed (HTTP {status})")
        return body

    # --------------------------------------------------------------- power
    def facility_power(self) -> dict:
        status, body = self.request("GET", "/api/v1/facility/power")
        if status != 200:
            raise DomainUnavailableError(f"facility power failed (HTTP {status})")
        return body
