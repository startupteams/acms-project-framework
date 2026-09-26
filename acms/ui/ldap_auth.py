"""LLDAP-backed human authentication for the web UI (plan §6).

Flow: service-account bind → user lookup (DN + ``memberOf``) → rebind as the
user with the supplied password to verify it → map groups to an ACMS role.

Failure semantics (fail closed):
- LDAP unreachable / misconfigured → ``LdapUnavailable`` → HTTP 503.
- Unknown user, wrong password, or no mapped ACMS group → ``role=None`` →
  denied with a generic "invalid credentials" message (no user enumeration).

Synchronous ldap3 calls run in a worker thread so FastAPI's async event loop
is never blocked (plan §8).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio

from ..settings import get_settings
from .roles import Role, highest_role

logger = logging.getLogger("acms.ui.ldap")


class LdapUnavailable(RuntimeError):
    """The LDAP service is unreachable or misconfigured — deny access."""


@dataclass(frozen=True)
class LdapAuthResult:
    username: str
    role: Role | None


def _authenticate_sync(username: str, password: str) -> LdapAuthResult:
    settings = get_settings()
    if not settings.ldap_url or not settings.ldap_user_base:
        raise LdapUnavailable("ACMS_LDAP_URL / ACMS_LDAP_USER_BASE are not configured")

    import ldap3  # imported lazily so unit tests can fake it and app import stays light
    from ldap3.utils.conv import escape_filter_chars

    server = ldap3.Server(settings.ldap_url, get_info=ldap3.NONE, connect_timeout=5)
    try:
        conn = ldap3.Connection(
            server,
            user=settings.ldap_bind_dn or None,
            password=settings.ldap_bind_password or None,
            auto_bind=True,
            raise_exceptions=True,
            receive_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — classify all bind failures as unavailable
        logger.warning("LDAP service bind failed: %s", exc.__class__.__name__)
        raise LdapUnavailable("LDAP bind failed") from exc

    try:
        with conn:
            escaped = escape_filter_chars(username)
            search_filter = settings.ldap_user_filter.replace("{username}", escaped)
            conn.search(
                settings.ldap_user_base,
                search_filter,
                attributes=["memberOf"],
                size_limit=1,
            )
            if not conn.entries:
                # Unknown user — deny with the same generic result as a bad password.
                return LdapAuthResult(username=username, role=None)

            entry = conn.entries[0]
            member_of = list(entry.entry_attributes_as_dict.get("memberOf", []))
            # LLDAP group objects expose membership via both member and uniqueMember.
            if not member_of:
                member_of = list(entry.entry_attributes_as_dict.get("member", []))

            # Verify the password by rebinding as the found user DN.
            try:
                if not conn.rebind(user=entry.entry_dn, password=password):
                    return LdapAuthResult(username=username, role=None)
            except Exception:  # noqa: BLE001 — a rejected user bind is a credential failure
                return LdapAuthResult(username=username, role=None)
    except LdapUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — classify search/protocol failures as unavailable
        logger.warning("LDAP search failed: %s", exc.__class__.__name__)
        raise LdapUnavailable("LDAP search failed") from exc

    return LdapAuthResult(username=username, role=highest_role(member_of))


async def authenticate(username: str, password: str) -> LdapAuthResult:
    """Run the synchronous LDAP flow in a worker thread."""
    if not username or not password:
        return LdapAuthResult(username=username, role=None)
    return await anyio.to_thread.run_sync(_authenticate_sync, username, password)
