"""Registry + Monitoring domain adapter (plan §29) — read-only context.

Two authorities:
- **registry**: MIAM Service Registry (VM119, ``https://registry.miam.home.arpa``,
  Bearer token). Services list/detail = company-internal service catalog.
- **monitoring**: Uptime Kuma via its socket.io surface. The verified wire
  protocol lives in the VM119 reconciler (``kuma_sync.py``): websocket login
  {username,password,token:""} + ack, then ``getMonitorList`` ack carries
  monitorList. Status/heartbeat reads ONLY — never add/edit/delete monitors.

Surface (plan §29):

Resources (READ):
  registry.services.list        (URI: registry://services)
  registry.service.get          (URI: registry://service/{service_id})
  registry.dependencies.get     (URI: registry://service/{service_id}/dependencies)
  monitoring.status             (URI: monitoring://status)
  monitoring.monitor.get        (URI: monitoring://monitor/{monitor_id})
  monitoring.incidents          (URI: monitoring://incidents)

Writes: NONE (plan §29 — registry/Kuma provide context and observability,
not authorization; every write surface is DENIED BY ABSENCE + a policy note).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .errors import DomainUnavailableError, NotFoundError
from .models import CallIdentity


class RegistryClient:
    """Thin MIAM Service Registry REST client (read-only)."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: int = 15):
        if not (base_url and token):
            raise ValueError("Registry base URL / token not configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout_seconds

    def _request(self, path: str) -> Any:
        req = urllib.request.Request(f"{self.base_url}{path}")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFoundError(f"registry record not found: {path}")
            raise DomainUnavailableError(f"registry read failed (HTTP {exc.code})")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DomainUnavailableError(f"registry unreachable: {exc}") from exc

    def list_services(self) -> list[dict[str, Any]]:
        body = self._request("/api/v1/services")
        services = body.get("services") if isinstance(body, dict) else body
        if not isinstance(services, list):
            raise DomainUnavailableError("registry returned unexpected payload")
        return services

    def get_service(self, service_id: str) -> dict[str, Any]:
        services = self.list_services()
        for s in services:
            if s.get("id") == service_id:
                return s
        raise NotFoundError(f"service not found: {service_id}")


class MonitoringClient:
    """Uptime Kuma read-only client over the verified socket.io protocol.

    Login handshake per kuma_sync.py (websocket transport + retry semantics);
    every method here is a READ (getMonitorList / heartbeat list). No
    add/edit/delete calls are ever emitted from this client.
    """

    def __init__(self, url: str, username: str, password: str):
        if not (url and username and password):
            raise ValueError("Kuma URL / credentials not configured")
        self.url = url.rstrip("/")
        self._username = username
        self._password = password

    def _with_session(self, fn) -> Any:
        try:
            import socketio
        except ImportError as exc:  # pragma: no cover
            raise DomainUnavailableError(
                "monitoring domain requires the socketio client package") from exc
        ack: dict[str, tuple] = {}

        def _ack_cb(key: str):
            def cb(*args):
                ack[key] = args
            return cb

        sio = socketio.Client(ssl_verify=False, logger=False, engineio_logger=False)
        try:
            sio.connect(self.url, transports=["websocket"], wait_timeout=20)
            time.sleep(2)
            sio.emit("login", {"username": self._username,
                               "password": self._password, "token": ""},
                     callback=_ack_cb("login"))
            deadline = time.time() + 10
            while "login" not in ack and time.time() < deadline:
                time.sleep(0.3)
            la = ack.get("login")
            login_ok = bool(la) and bool(la[0].get("ok")) if la else False
            if not login_ok:
                raise DomainUnavailableError("monitoring login failed")
            return fn(sio, _ack_cb)
        finally:
            try:
                sio.disconnect()
            except Exception:
                pass

    def list_monitors(self) -> list[dict[str, Any]]:
        def read(sio, _ack_cb):
            result: dict[str, tuple] = {}
            sio.emit("getMonitorList", callback=_ack_cb(result, "list"))
            deadline = time.time() + 8
            while "list" not in result and time.time() < deadline:
                time.sleep(0.3)
            entry = result.get("list")
            d = entry[0] if entry else {}
            ml = d.get("monitorList", {}) if isinstance(d, dict) else {}
            if isinstance(ml, dict) and "monitorList" in ml:
                ml = ml["monitorList"]
            items = list(ml.values()) if isinstance(ml, dict) else list(ml)
            return sorted([
                {"id": m.get("id"), "name": m.get("name"),
                 "type": m.get("type"), "url": m.get("url"),
                 "interval": m.get("interval"), "active": m.get("active"),
                 "upside_down": m.get("upsideDown", False)}
                for m in items], key=lambda x: x["id"] or 0)
        return self._with_session(read)

    def get_monitor(self, monitor_id: int) -> dict[str, Any]:
        monitors = self.list_monitors()
        for m in monitors:
            if str(m.get("id")) == str(monitor_id):
                return m
        raise NotFoundError(f"monitor not found: {monitor_id}")

    def incidents(self) -> list[dict[str, Any]]:
        """Down/inactive monitors = the incident view (honest minimal)."""
        out = []
        for m in self.list_monitors():
            beat = m.get("last_beat") or {}
            status = beat.get("status")
            if not m.get("active") or status in (0,):
                out.append({"monitor_id": m.get("id"), "name": m.get("name"),
                            "active": m.get("active"), "beat_status": status})
        return out