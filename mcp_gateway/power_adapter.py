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

from .errors import NotFoundError
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

    def pdu_asset_power_state(self, asset_id: str) -> dict:
        """Per-asset power state via SM (GET /api/v1/pdu/assets/{id}/power-state)."""
        status, body = self.request(
            "GET", f"/api/v1/pdu/assets/{asset_id}/power-state")
        if status == 404:
            raise NotFoundError(f"unknown pdu asset: {asset_id}")
        if status != 200:
            raise DomainUnavailableError(
                f"pdu asset power-state failed (HTTP {status})")
        return body

    def pdu_outlet_status(self, pdu_key: str, outlet: int) -> dict:
        """Outlet state keyed by the owning asset (plan §27 surface).

        W5 live-found: SM exposes /api/v1/pdu/assets/{id}/power-state (the
        PDU Manager contract); /api/v1/pdus/{key}/outlets/{n} does NOT exist
        on SM (that path is the PDU Manager's own API) and always 404'd.
        Resolution: match the outlet by (pdu_id, outlet) in the asset index
        and return that asset's power-state.
        """
        status, assets = self.request("GET", "/api/v1/pdu/assets")
        if status != 200:
            raise DomainUnavailableError(
                f"pdu assets failed (HTTP {status}) while resolving outlet")
        match = next((a for a in (assets.get("data") or [])
                      if str(a.get("pdu_id")) == str(pdu_key)
                      and str(a.get("outlet")) == str(int(outlet))), None)
        if match is None:
            raise NotFoundError(f"unknown pdu/outlet: {pdu_key}/{outlet}")
        state = self.pdu_asset_power_state(match["asset_id"])
        return {"pdu": pdu_key, "outlet": int(outlet),
                "asset_id": match.get("asset_id"),
                "label": match.get("label"),
                "protected": match.get("protected"),
                "state": state.get("power_state") or state.get("state"),
                "source": "sm:/api/v1/pdu/assets/{id}/power-state"}

    # --------------------------------------------------------------- power
    def facility_power(self) -> dict:
        status, body = self.request("GET", "/api/v1/facility/power")
        if status != 200:
            raise DomainUnavailableError(f"facility power failed (HTTP {status})")
        return body
