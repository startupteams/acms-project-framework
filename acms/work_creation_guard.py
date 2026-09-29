"""Work Item creation authority (ADR-0011 Accepted; execution plan §1.4/Phase C).

Canonical semantics (human-accepted 2026-09-29):

    one ACMS Work Item = one measurable goal with a measurable deliverable

Creation authority:

    Administrator (bearer admin token)  -> may create Work Items
    Executive Agent (delegated scope)   -> may create Work Items
    ordinary Worker agent               -> may NOT create Work Items
    Observer                            -> may NOT create Work Items

Executive identity is configuration-backed (``ACMS_EXECUTIVE_AGENT_IDS``),
never inferred from a display name. Subordinate workers who identify new work
record a ``proposed_next_work`` block in their handoff; the Executive Agent
decides whether it becomes a Work Item.
"""

from __future__ import annotations

from dataclasses import dataclass

from acms.settings import get_settings


@dataclass(frozen=True)
class CreationDecision:
    allowed: bool
    reason: str
    authority: str  # "administrator" | "executive" | "worker" | "unknown"


def executive_agent_ids() -> set[str]:
    """Parse the configured Executive Agent identity list (comma-separated)."""
    raw = get_settings().executive_agent_ids
    return {item.strip() for item in raw.split(",") if item.strip()}


def evaluate_work_item_creation(*, caller_is_admin: bool, created_by: str | None) -> CreationDecision:
    """Decide whether a Work Item creation request may proceed.

    ``caller_is_admin`` reflects bearer-token authentication (the
    ``require_admin_token`` dependency passed). ``created_by`` is the claimed
    provenance string; ``agent:<id>`` values are checked against the
    configured Executive identity list.
    """
    if caller_is_admin:
        # Administrative creation is always allowed. If the request claims an
        # agent identity, verify it is the Executive so mislabeled provenance
        # cannot launder worker creation through the admin path.
        if created_by and created_by.startswith("agent:"):
            agent_id = created_by.removeprefix("agent:")
            if agent_id not in executive_agent_ids():
                return CreationDecision(
                    allowed=False,
                    reason=(
                        "created_by claims agent identity '%s' which is not a configured "
                        "Executive Agent (ACMS_EXECUTIVE_AGENT_IDS); workers must record "
                        "proposed_next_work in a handoff instead of creating Work Items "
                        "(ADR-0011)" % agent_id
                    ),
                    authority="worker",
                )
        return CreationDecision(allowed=True, reason="administrator authority", authority="administrator")

    # Non-admin caller: only a configured Executive Agent identity may create.
    if created_by and created_by.startswith("agent:"):
        agent_id = created_by.removeprefix("agent:")
        if agent_id in executive_agent_ids():
            return CreationDecision(allowed=True, reason="executive delegated scope", authority="executive")
        return CreationDecision(
            allowed=False,
            reason=(
                "agent '%s' is not a configured Executive Agent; Work Item creation is "
                "Executive/Administrator-only (ADR-0011). Record proposed_next_work in a "
                "handoff for Executive review." % agent_id
            ),
            authority="worker",
        )

    return CreationDecision(
        allowed=False,
        reason="unknown creator provenance; Work Item creation requires administrator or Executive authority (ADR-0011)",
        authority="unknown",
    )
