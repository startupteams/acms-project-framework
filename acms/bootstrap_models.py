"""Product-idea bootstrap durability (window-5 plan §9.5).

One row per bootstrap request; ``steps_json`` carries per-step durable results
(repository, baseline docs, Jira linkage, ACMS hierarchy, initial Work) so
repeated clicks / timeouts / retries REUSE previously created resources instead
of duplicating them. Ambiguous remote-creation outcomes reconcile by stored
correlation metadata (repo name + request id) before any retry; if uncertainty
persists the request stays awaiting-operator — nothing is deleted to compensate.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BootstrapRequestRecord(Base):
    """Resumable product-bootstrap request (§9.5)."""

    __tablename__ = "bootstrap_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    # pasted_markdown | github_idea | blank
    source_kind: Mapped[str] = mapped_column(String(32))
    # draft | awaiting_jira | completed | failed | awaiting_operator
    state: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # per-step durable results: {"repository": {...}, "baseline_docs": {...}, ...}
    steps_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    idea_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                 onupdate=_now)