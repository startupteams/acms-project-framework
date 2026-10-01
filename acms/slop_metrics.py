"""Repository entity + Slop Ratio metrics (STEA-004 plan §15/§16).

Repository = durable entity for a git repository (owner/repo + canonical URL),
linkable to Products AND Projects (many-to-many via link tables — one repo may
belong to multiple contexts without duplicate rows).

Slop Ratio v1 (no thresholds — baseline collection first, §16):
    True Slop Ratio      = LOC / human-written ADRs
    Precision Slop Ratio = LOC / total ADRs
Human-written ADR v1 heuristic (§16.1): an ADR is human-written when its file
contains or links at least one durable human-authored decision — detected via
the ADR body containing a human-acceptance marker. Markers checked, in order:
    - front-matter/status line: "Accepted" with a human reviewer reference
    - explicit ownership tags: "human:", "owner:", "jordan", "approved-by"
Everything else counts as total-only. Counts are stored SEPARATELY
(human_written_adr_count, human_answer_count) so the heuristic can be refined
later without losing history (§16.1).

LOC rules (§16): language-aware line counting EXCLUDING .git, vendor,
node_modules, venv, build output, vendored code. Measured via the GitHub API
(git trees + raw contents) — the ACMS app container has no git binary, so a
git-clone collector would not run in production. Exact exclusions documented
in EXCLUDED_DIRS/PATTERNS.

Refresh: POST /api/v1/repository-metrics/refresh (manual, on-demand) +
metrics stored as snapshots (repository_metric_snapshot) with full timeseries
history preserved for charts. Hourly scheduler hook is a plan §16 default —
wired via the existing telemetry scheduler tick (non-blocking).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Base

GITHUB_API = "https://api.github.com"

# §16 LOC exclusions — documented exact list
EXCLUDED_DIRS = {
    ".git", "vendor", "vendors", "node_modules", "venv", ".venv", "env",
    ".env", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    "target", "out", ".next", ".nuxt", "coverage", ".tox", ".eggs",
    "site-packages", ".terraform", "vendor.bundle",
}
EXCLUDED_SUFFIXES = {
    ".min.js", ".min.css", ".map", ".lock", ".sum", ".pyc", ".pyo",
    ".so", ".o", ".a", ".dll", ".exe", ".bin", ".woff", ".woff2", ".ttf",
    ".eot", ".otf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".pdf", ".zip", ".gz", ".tgz", ".tar", ".bz2", ".7z", ".mp4", ".mp3",
    ".sqlite", ".db", ".dump", ".parquet", ".feather", ".h5", ".pt",
    ".pth", ".onnx", ".gguf", ".safetensors",
}
ADR_SUFFIXES = (".md", ".mdx")
ADR_DIR_HINTS = ("adr", "decisions", "decision-records")

# Human-acceptance markers for the v1 human-written ADR heuristic (§16.1)
HUMAN_ADR_MARKERS = (
    re.compile(r"status:\s*accepted.*human", re.IGNORECASE),
    re.compile(r"\b(approved[- ]by|accepted[- ]by|reviewed[- ]by|decided[- ]by)\s*[:\-]?\s*\S+", re.IGNORECASE),
    re.compile(r"\bhuman\s*(approved|accepted|authored|decision)\b", re.IGNORECASE),
    re.compile(r"\bjordan\b", re.IGNORECASE),  # product-owner name per agents.md convention
)


def _token() -> str:
    return os.environ.get("ACMS_GITHUB_ECONOMICS_TOKEN", "")


def _gh(method: str, path: str, timeout: int = 30) -> tuple[int, dict | list | None, str]:
    req = urllib.request.Request(GITHUB_API + path, method=method)
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {}), ""
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = {}
        return e.code, detail, str(detail.get("message", ""))[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, None, str(exc)[:200]


def is_code_file(path: str) -> bool:
    low = path.lower()
    if low.endswith(tuple(EXCLUDED_SUFFIXES)):
        return False
    parts = low.split("/")
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return False
    # count every text-like file; the tree API already excludes nothing, so
    # this filter decides LOC-worthiness per §16 (exclude vendor/generated)
    return True


def is_adr_file(path: str) -> bool:
    low = path.lower()
    if not low.endswith(ADR_SUFFIXES):
        return False
    parts = low.split("/")
    return any(h in p for p in parts for h in ADR_DIR_HINTS) or parts[-1].startswith(("adr", "adr-"))


def count_loc(content: str) -> int:
    """Language-aware-enough v1 LOC: non-empty, non-comment-only lines."""
    n = 0
    in_block_comment = False
    block_start = ""
    for ln in content.splitlines():
        s = ln.strip()
        if in_block_comment:
            if block_start and s.endswith(block_end_ := block_start):
                in_block_comment = False
            continue
        if not s:
            continue
        if s.startswith(("#", "//", "--", ";", "%")):
            continue  # line comments (py/sh/sql/lua)
        if s.startswith("/*"):
            in_block_comment = True
            block_start = "*/"
            continue
        if s.startswith("<!--"):
            in_block_comment = True
            block_end_ = "-->"
            continue
        if s.startswith('"""') or s.startswith("'''"):
            tok = s[:3]
            if s.count(tok) >= 2 and len(s) > 3:
                continue  # single-line docstring
            in_block_comment = True
            block_start = tok
            continue
        n += 1
    return n


# ---------------------------------------------------------------- ORM models

from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402
from sqlalchemy import String, Integer, Float, DateTime, UniqueConstraint  # noqa: E402


class RepositoryRecord(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner", "repo", name="uq_repositories_owner_repo"),)

    repository_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    repo: Mapped[str] = mapped_column(String(128))
    canonical_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    default_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # measurement state (§15)
    last_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_loc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_total_adr_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_human_adr_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class ProductRepositoryLink(Base):
    __tablename__ = "product_repository"
    __table_args__ = (UniqueConstraint("product_id", "repository_id",
                                       name="uq_product_repository"),)

    product_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ProjectRepositoryLink(Base):
    __tablename__ = "project_repository"
    __table_args__ = (UniqueConstraint("project_id", "repository_id",
                                       name="uq_project_repository"),)

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RepositoryMetricSnapshot(Base):
    """Timeseries per §16: timestamp, LOC, ADR counts, ratios, commit SHAs."""

    __tablename__ = "repository_metric_snapshot"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(36), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    loc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_adr_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_written_adr_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_answer_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    true_slop_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    precision_slop_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    @staticmethod
    def new_id() -> str:
        import uuid

        return str(uuid.uuid4())


# ---------------------------------------------------------------- measurement


def _fetch_default_branch(owner: str, repo: str) -> tuple[str | None, str]:
    st, data, err = _gh("GET", f"/repos/{owner}/{repo}")
    if st != 200 or not isinstance(data, dict):
        return None, err or f"repos API {st}"
    return data.get("default_branch"), ""


def _fetch_tree(owner: str, repo: str, branch: str) -> tuple[list[dict] | None, str]:
    st, data, err = _gh("GET", f"/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    if st != 200 or not isinstance(data, dict):
        return None, err or f"trees API {st}"
    return [t for t in data.get("tree", []) if t.get("type") == "blob"], ""


def _fetch_file(owner: str, repo: str, path: str, ref: str) -> str | None:
    import urllib.parse

    q = urllib.parse.quote(path, safe="/")
    st, data, _ = _gh("GET", f"/repos/{owner}/{repo}/contents/{q}?ref={ref}")
    if st != 200 or not isinstance(data, dict):
        return None
    enc = data.get("encoding")
    if enc == "base64":
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if data.get("content"):
        return str(data["content"])
    return None


def measure_repository(owner: str, repo: str) -> dict:
    """Measure one repo via GitHub API. Returns a snapshot dict (never raises
    for API failures — carries error + partial data)."""
    snap: dict = {"loc": None, "total_adr_count": None, "human_written_adr_count": None,
                  "human_answer_count": 0, "commit_sha": None, "error": None}
    branch, err = _fetch_default_branch(owner, repo)
    if branch is None:
        snap["error"] = f"default branch: {err}"[:255]
        return snap
    tree, err = _fetch_tree(owner, repo, branch)
    if tree is None:
        snap["error"] = f"tree: {err}"[:255]
        return snap
    snap["commit_sha"] = (tree and tree[0].get("sha")) or None  # proxy; refined below
    # Real HEAD sha:
    st, head, _ = _gh("GET", f"/repos/{owner}/{repo}/commits/{branch}")
    if st == 200 and isinstance(head, dict):
        snap["commit_sha"] = head.get("sha")
    code_paths = [t["path"] for t in tree
                  if is_code_file(t["path"]) and not is_adr_file(t["path"])]
    adr_paths = [t["path"] for t in tree if is_adr_file(t["path"])]
    if len(code_paths) > 60:
        snap["error"] = (f"repository too large for v1 collector ({len(code_paths)} files > cap 60)")[:255]
        return snap
    loc = 0
    for p in code_paths:
        content = _fetch_file(owner, repo, p, branch)
        if content is not None:
            loc += count_loc(content)
    total_adr = 0
    human_adr = 0
    for p in adr_paths:
        content = _fetch_file(owner, repo, p, branch)
        if content is None:
            continue
        total_adr += 1
        if any(m.search(content) for m in HUMAN_ADR_MARKERS):
            human_adr += 1
    snap.update({"loc": loc, "total_adr_count": total_adr,
                 "human_written_adr_count": human_adr,
                 "human_answer_count": human_adr})  # v1: answers == human ADRs (§16.1 stored separately)
    return snap


def compute_ratios(loc: int | None, total_adr: int | None, human_adr: int | None) -> tuple[float | None, float | None]:
    """True = LOC/human ADRs; Precision = LOC/total ADRs. None when divisor 0/None."""
    true_r = precision_r = None
    if loc is not None and human_adr:
        true_r = round(loc / human_adr, 2)
    if loc is not None and total_adr:
        precision_r = round(loc / total_adr, 2)
    return true_r, precision_r


async def get_or_create_repository(db: AsyncSession, owner: str, repo: str,
                                   canonical_url: str | None = None) -> "RepositoryRecord":
    row = (await db.scalars(
        select(RepositoryRecord).where(RepositoryRecord.owner == owner,
                                       RepositoryRecord.repo == repo).limit(1)
    )).first()
    if row:
        return row
    rec = RepositoryRecord(
        repository_id=RepositoryRecord.new_id(), owner=owner, repo=repo,
        canonical_url=canonical_url or f"https://github.com/{owner}/{repo}")
    db.add(rec)
    await db.flush()
    return rec


async def refresh_repository_metrics(db: AsyncSession, repository_id: str | None = None) -> list[dict]:
    """Measure linked repos (one, or all when repository_id None) + store snapshots."""
    stmt = select(RepositoryRecord)
    if repository_id:
        stmt = stmt.where(RepositoryRecord.repository_id == repository_id)
    repos = (await db.scalars(stmt)).all()
    out = []
    for r in repos:
        measured = measure_repository(r.owner, r.repo)
        true_r, precision_r = compute_ratios(
            measured.get("loc"), measured.get("total_adr_count"),
            measured.get("human_written_adr_count"))
        snap = RepositoryMetricSnapshot(
            snapshot_id=RepositoryMetricSnapshot.new_id(),
            repository_id=r.repository_id,
            captured_at=datetime.now(timezone.utc),
            commit_sha=measured.get("commit_sha"),
            loc=measured.get("loc"),
            total_adr_count=measured.get("total_adr_count"),
            human_written_adr_count=measured.get("human_written_adr_count"),
            human_answer_count=measured.get("human_answer_count") or 0,
            true_slop_ratio=true_r,
            precision_slop_ratio=precision_r,
            error=measured.get("error"),
        )
        db.add(snap)
        r.last_commit_sha = measured.get("commit_sha")
        r.last_measured_at = snap.captured_at
        r.last_loc = measured.get("loc")
        r.last_total_adr_count = measured.get("total_adr_count")
        r.last_human_adr_count = measured.get("human_written_adr_count")
        out.append({"repository_id": r.repository_id, "slug": r.slug,
                    "loc": measured.get("loc"), "total_adr": measured.get("total_adr_count"),
                    "human_adr": measured.get("human_written_adr_count"),
                    "true_slop_ratio": true_r, "precision_slop_ratio": precision_r,
                    "error": measured.get("error"), "snapshot_id": snap.snapshot_id})
    await db.commit()
    return out


async def product_slop_summary(db: AsyncSession, product_id: str) -> dict:
    """Aggregate unique-source Slop summary for one Product (§16 no double-count)."""
    from .a2a_models import ProductRecord

    product = await db.get(ProductRecord, product_id)
    if product is None:
        return {}
    rows = (await db.scalars(
        select(ProductRepositoryLink)
        .where(ProductRepositoryLink.product_id == product_id))).all()
    repo_ids = [r.repository_id for r in rows]
    # unique-source rule: dedupe repository_ids (link table already unique, but
    # a repo could be linked via multiple projects under the product — §16)
    latest = {}
    if repo_ids:
        snaps = (await db.scalars(
            select(RepositoryMetricSnapshot)
            .where(RepositoryMetricSnapshot.repository_id.in_(set(repo_ids)))
            .order_by(RepositoryMetricSnapshot.captured_at.desc()))).all()
        for s in snaps:
            latest.setdefault(s.repository_id, s)  # first per repo = newest
    total_loc = sum(s.loc or 0 for s in latest.values())
    total_adr = sum(s.total_adr_count or 0 for s in latest.values())
    human_adr = sum(s.human_written_adr_count or 0 for s in latest.values())
    true_r, precision_r = compute_ratios(total_loc, total_adr, human_adr)
    return {
        "product_id": product_id, "product_name": product.name,
        "repository_count": len(latest),
        "loc": total_loc, "total_adr_count": total_adr,
        "human_written_adr_count": human_adr,
        "true_slop_ratio": true_r, "precision_slop_ratio": precision_r,
        "last_measured_at": max((s.captured_at for s in latest.values()), default=None),
    }


async def product_slop_history(db: AsyncSession, product_id: str) -> list[dict]:
    """Timeseries for charts (§16): union of member-repo snapshots."""
    rows = (await db.scalars(
        select(ProductRepositoryLink)
        .where(ProductRepositoryLink.product_id == product_id))).all()
    repo_ids = list({r.repository_id for r in rows})
    if not repo_ids:
        return []
    snaps = (await db.scalars(
        select(RepositoryMetricSnapshot)
        .where(RepositoryMetricSnapshot.repository_id.in_(repo_ids))
        .order_by(RepositoryMetricSnapshot.captured_at.asc()))).all()
    return [{
        "captured_at": s.captured_at, "repository_id": s.repository_id,
        "loc": s.loc, "total_adr_count": s.total_adr_count,
        "human_written_adr_count": s.human_written_adr_count,
        "true_slop_ratio": s.true_slop_ratio,
        "precision_slop_ratio": s.precision_slop_ratio,
        "commit_sha": s.commit_sha,
    } for s in snaps]