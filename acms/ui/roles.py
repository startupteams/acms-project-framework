"""Human access roles (ACMS-REQ-023) and LLDAP group mapping.

LLDAP groups map to ACMS roles via configured group DNs. Group membership is
only read — LLDAP is never modified by ACMS (plan §6: no automatic directory
changes). A user who authenticates but has no mapped group is denied.
"""
from __future__ import annotations

from enum import StrEnum

from ..settings import get_settings


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    WORKER = "worker"
    OBSERVER = "observer"


_ROLE_RANK: dict[Role, int] = {
    Role.OBSERVER: 1,
    Role.WORKER: 2,
    Role.ADMINISTRATOR: 3,
}


def highest_role(member_of_dns: list[str]) -> Role | None:
    """Map LLDAP group DNs to the user's highest ACMS role.

    Unmapped or unmapped-configured users get ``None`` (denied). If a user is
    in several mapped groups, the highest-privilege role wins so behavior is
    deterministic.
    """
    settings = get_settings()
    configured: dict[str, Role] = {
        settings.ldap_group_admin: Role.ADMINISTRATOR,
        settings.ldap_group_worker: Role.WORKER,
        settings.ldap_group_observer: Role.OBSERVER,
    }
    normalized = {dn.strip().lower() for dn in member_of_dns if dn}
    best: Role | None = None
    for group_dn, role in configured.items():
        if not group_dn:
            continue
        if group_dn.strip().lower() in normalized:
            if best is None or _ROLE_RANK[role] > _ROLE_RANK[best]:
                best = role
    return best


# All currently implemented UI pages are read views available to every mapped
# role (plan §6: Administrator all pages, Worker read views, Observer read-only).
# Privileged action buttons arrive only with backend operations + authz (§19).
