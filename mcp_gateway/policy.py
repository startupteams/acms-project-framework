"""Gateway policy engine (plan §16/§17/§19).

Contract for every capability (tool or resource):

- ``risk``: READ | SAFE_WRITE | SENSITIVE_WRITE | DESTRUCTIVE
- ``roles``: minimum role set (any-of)
- ``scopes``: minimum scope set (any-of)
- DESTRUCTIVE + caller without executive/infrastructure_admin => hard DENY
- SENSITIVE_WRITE + worker => APPROVAL_REQUIRED (W1: denied-with-reason until
  the approval workflow exists in W2+; policy-gated per plan)

Tool names use dotted namespaces (``acms.work.update_progress``). Tool-manager
storage keys normalize dots to underscores (SDK constraint); the canonical
dotted name is preserved in ``canonical_name`` and surfaces in tools/list via
the registry layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import (
    ApprovalRequiredError,
    DestructiveDenyError,
    RoleRequiredError,
    ScopeRequiredError,
)
from .models import CallIdentity, RiskClass

SENSITIVE_ROLES = {"executive", "infrastructure_admin"}


@dataclass
class Capability:
    """One policy-annotated capability (tool or resource factory)."""

    name: str                      # canonical dotted name (tools) or URI template (resources)
    domain: str                    # acms | llm | runtime | proxmox | github | pdu | power | jira | registry | monitoring
    risk: RiskClass
    roles: set[str]
    scopes: set[str]
    description: str = ""
    version: int = 1
    timeout_seconds: int = 10
    idempotent: bool = True
    side_effect: str = "none"      # none | local | external
    audit: bool = True
    handler: Callable[..., Any] | None = None
    kind: str = "tool"             # tool | resource
    uri_template: str | None = None
    output_schema: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def authorize(self, identity: CallIdentity) -> None:
        """Raise typed errors unless the identity may call this capability."""
        if self.risk == RiskClass.DESTRUCTIVE:
            # W1 hard rule: DESTRUCTIVE is denied for EVERYONE through the
            # gateway (executive path exists only via an audited emergency
            # policy — not implemented yet). Plan §16/§43.
            raise DestructiveDenyError(
                f"{self.name} is DESTRUCTIVE; denied at the gateway (W1 policy)"
            )
        if self.risk == RiskClass.SENSITIVE_WRITE and not identity.has_role(*SENSITIVE_ROLES):
            # W1: no approval workflow yet — fail closed with an actionable reason.
            raise ApprovalRequiredError(
                f"{self.name} is SENSITIVE_WRITE and requires approval/role gate "
                f"(caller roles: {','.join(identity.roles) or 'none'})"
            )
        if self.risk == RiskClass.SENSITIVE_WRITE and identity.has_role(*SENSITIVE_ROLES):
            return  # executives/admins ARE the approval authority (plan §16)
        if self.roles and not identity.has_role(*self.roles):
            raise RoleRequiredError(
                f"{self.name} requires one of {sorted(self.roles)}; "
                f"caller has {sorted(identity.roles)}"
            )
        if self.scopes and not any(s in identity.scopes for s in self.scopes):
            raise ScopeRequiredError(
                f"{self.name} requires one of {sorted(self.scopes)}; "
                f"caller has {sorted(identity.scopes)}"
            )


def dotted(name: str) -> str:
    """Storage-key normalization: dots -> underscores (SDK schema constraint)."""
    return name.replace(".", "_")


def canonical(dotted_name: str) -> str:
    """Best-effort dotted canonical name from a normalized storage key."""
    return dotted_name.replace("_", ".", 1) if "_" in dotted_name and "." not in dotted_name else dotted_name


class PolicyRegistry:
    """Central capability registry (plan §37) with authorization checks."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, cap: Capability) -> Capability:
        if cap.kind == "tool":
            key = dotted(cap.name)
        else:
            key = cap.name  # resources keyed by URI template
        if key in self._capabilities:
            raise ValueError(f"duplicate capability key: {key}")
        self._capabilities[key] = cap
        return cap

    def tool_for_storage_key(self, storage_key: str) -> Capability | None:
        return self._capabilities.get(storage_key)

    def resource_for_uri(self, uri: str) -> tuple[Capability, dict[str, str]] | None:
        """Match a concrete resource URI against registered URI templates."""
        for cap in self._capabilities.values():
            if cap.kind != "resource" or not cap.uri_template:
                continue
            params = match_uri_template(cap.uri_template, uri)
            if params is not None:
                return cap, params
        return None

    def list_capabilities(self) -> list[Capability]:
        return sorted(self._capabilities.values(), key=lambda c: (c.domain, c.kind, c.name))

    def __len__(self) -> int:
        return len(self._capabilities)


import re  # noqa: E402

_TEMPLATE_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def match_uri_template(template: str, uri: str) -> dict[str, str] | None:
    """Match ``acms://work/{work_uid}`` against a concrete URI. Returns params
    or None. Segment counts must match; params never span '/'; url-decoding
    is the caller's job (URIs here are already percent-decoded by MCP)."""
    tparts = template.strip("/").split("/")
    uparts = uri.strip("/").split("/")
    if len(tparts) != len(uparts):
        return None
    params: dict[str, str] = {}
    for tp, up in zip(tparts, uparts):
        m = _TEMPLATE_PARAM.fullmatch(tp)
        if m:
            params[m.group(1)] = up
        elif tp != up:
            return None
    return params
