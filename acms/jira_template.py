"""STNA-86 Jira template fidelity validation (STEA-004 plan §10).

History: STNA-87 (the prior acceptance ticket) was created as a one-time
approximation of STNA-86 rather than a faithful clone — a process defect this
module prevents repeating.

Contract for FUTURE explicitly-authorized AI-created Jira tickets:

1. CLONE STNA-86 — never approximate the template from memory.
2. Fetch the live STNA-86 issue fields/description as the structural source.
3. Compare required sections against the candidate ticket description.
4. FAIL validation when required sections are absent from the candidate.

No ordinary ACMS autonomous ticket creation — validation only runs when a
human explicitly authorizes a ticket clone (Jira write path stays gated).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Template issues that define the required structural sections.
TEMPLATE_ISSUE_KEYS = ("STNA-86",)

# Sections STNA-86 must carry (h2/h3 headings, matched case-insensitively).
# Derived from the live ticket at validation time — the FROZEN list below is
# the minimal acceptance-structure floor used when the live fetch is a MOCK
# or unavailable (validation then reports limited-confidence).
REQUIRED_SECTION_FLOOR = (
    "bluf",
    "objective",
    "scope",
    "acceptance",
)


@dataclass
class TemplateValidationResult:
    source_key: str = ""
    source_fetch_ok: bool = False
    source_sections: list[str] = field(default_factory=list)
    candidate_sections: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    ok: bool = False
    notes: list[str] = field(default_factory=list)


def extract_sections(markdown_or_jira: str) -> list[str]:
    """Extract heading texts (## / ### / h2 / h3) from a Jira description."""
    sections = []
    for ln in (markdown_or_jira or "").splitlines():
        m = re.match(r"^\s*h[23]\.\s*(.+)$", ln.strip(), re.IGNORECASE)
        if m:
            sections.append(m.group(1).strip().lower())
            continue
        m = re.match(r"^\s*#{2,3}\s+(.+)$", ln.strip())
        if m:
            sections.append(m.group(1).strip().lower())
    return sections


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).strip()


async def validate_template_fidelity(
    client,
    source_key: str = "STNA-86",
    candidate_description: str | None = None,
    candidate_key: str | None = None,
) -> TemplateValidationResult:
    """Validate a candidate description against the live template issue.

    ``client`` is a JiraClient (real or mock). Fetches the SOURCE issue
    structure live, extracts its sections, and requires that every section
    whose normalized name matches the acceptance floor exists in the
    candidate. Additional source sections are reported as notes only.
    """
    result = TemplateValidationResult(source_key=source_key)
    try:
        src = client.get_issue(source_key)
        result.source_fetch_ok = True
        result.source_sections = extract_sections(src.description or "")
    except Exception as exc:  # noqa: BLE001 — fetch failure = limited-confidence
        result.source_fetch_ok = False
        result.notes.append(f"source fetch failed: {str(exc)[:120]}")

    effective_required = (
        [s for s in result.source_sections if any(f in _norm(s) for f in REQUIRED_SECTION_FLOOR)]
        or list(REQUIRED_SECTION_FLOOR)
    )
    if not result.source_fetch_ok:
        result.notes.append("using REQUIRED_SECTION_FLOOR as required list (source unavailable)")

    if candidate_description is None:
        if candidate_key:
            try:
                cand = client.get_issue(candidate_key)
                candidate_description = cand.description or ""
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"candidate fetch failed: {str(exc)[:120]}")
        if candidate_description is None:
            result.missing_required = list(effective_required)
            result.ok = False
            result.notes.append("no candidate description provided — cannot validate")
            return result

    result.candidate_sections = extract_sections(candidate_description)
    cand_norm = {_norm(s) for s in result.candidate_sections}
    for req in effective_required:
        if not any(_norm(req) in c or c in _norm(req) for c in cand_norm):
            result.missing_required.append(req)
    result.ok = not result.missing_required
    return result