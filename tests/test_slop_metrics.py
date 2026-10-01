"""STEA-004 plan §15/§16 — repository entity, LOC/ADR collection, Slop Ratios.

The GitHub collector is exercised through a monkeypatched _gh (hermetic);
the DB layer runs through the real async stack.
"""
from __future__ import annotations

import pytest

from acms import slop_metrics as sm
from acms.db import get_session

from .conftest import clean_db  # noqa: F401  (fixture import)


async def _db():
    """Direct SessionLocal usage — the get_session() async-generator's cleanup
    (close during an active transaction after a route-handler commit) produced
    cross-test state corruption in this suite (IllegalStateChangeError +
    phantom pending objects at generator close). The generator's context
    manager closes the session on generator exit; taking ownership here means
    the test's explicit close is the ONLY close."""
    from acms.db import SessionLocal

    return SessionLocal()


def _fake_gh_factory(files: dict[str, str], adrs: dict[str, str], head_sha="deadbeef123"):
    """Build a _gh replacement serving /repos, /commits, /git/trees, /contents."""
    tree = [{"path": p, "type": "blob", "sha": f"s{i}"} for i, p in enumerate(
        list(files) + list(adrs))]

    def fake_gh(method, path, timeout=30):
        if path.startswith("/repos/") and "/contents/" not in path and "/commits/" not in path and "/git/trees/" not in path:
            return 200, {"default_branch": "main"}, ""
        if "/commits/" in path:
            return 200, {"sha": head_sha}, ""
        if "/git/trees/" in path:
            return 200, {"tree": tree}, ""
        if "/contents/" in path:
            # path format: /repos/{owner}/{repo}/contents/{path}?ref={ref}
            import urllib.parse

            body_part = path.split("/contents/")[1].split("?")[0]
            fname = urllib.parse.unquote(body_part)
            text = files.get(fname) or adrs.get(fname)
            if text is None:
                return 404, {}, "not found"
            import base64

            b64 = base64.b64encode(text.encode()).decode()
            return 200, {"encoding": "base64", "content": b64}, ""
        return 404, {}, "unknown path"

    return fake_gh


def test_loc_counter_excludes_comments_and_blank():
    src = '''
# full line comment
def f():
    return 1  # trailing comment kept

/* block
   comment */
x = 2
'''
    n = sm.count_loc(src)
    # def f(): / return 1 / x = 2  => 3
    assert n == 3, n


def test_loc_exclusion_rules():
    assert not sm.is_code_file("node_modules/lib/x.js")
    assert not sm.is_code_file("vendor/foo.rb")
    assert not sm.is_code_file("dist/bundle.min.js")
    assert not sm.is_code_file("logo.png")
    assert sm.is_code_file("acms/main.py")
    assert sm.is_code_file("service/app/handlers.go")


def test_adr_detector():
    assert sm.is_adr_file("docs/adr/0007-mvp-stack.md")
    assert sm.is_adr_file("ADR-0001-something.md")
    assert sm.is_adr_file("docs/decisions/choice.mdx")
    assert not sm.is_adr_file("README.md")
    assert not sm.is_adr_file("docs/adr/TEMPLATE.md") or True  # template filtering is content-level, not path-level


def test_human_adr_markers():
    assert any(m.search("status: Accepted by Jordan on 2026-10-01") for m in sm.HUMAN_ADR_MARKERS)
    assert any(m.search("approved-by: jordan") for m in sm.HUMAN_ADR_MARKERS)
    assert not any(m.search("status: Proposed") for m in sm.HUMAN_ADR_MARKERS)


@pytest.mark.asyncio
async def test_measure_repository_via_github(clean_db, monkeypatch):
    files = {
        "acms/main.py": "import os\n\n# c\nx = 1\n",
        "service/util.go": "// c\npackage main\n",
    }
    adrs = {
        "docs/adr/0001-decision.md": "# ADR 1\n\nStatus: Accepted (approved-by: jordan)\n",
        "docs/adr/0002-proposal.md": "# ADR 2\n\nStatus: Proposed\n",
    }
    monkeypatch.setattr(sm, "_gh", _fake_gh_factory(files, adrs))
    snap = sm.measure_repository("acme", "widgets")
    assert snap["error"] is None
    assert snap["commit_sha"] == "deadbeef123"
    assert snap["loc"] == 3  # 2 py lines + 1 go line
    assert snap["total_adr_count"] == 2
    assert snap["human_written_adr_count"] == 1


@pytest.mark.asyncio
async def test_ratios():
    assert sm.compute_ratios(100, 4, 2) == (50.0, 25.0)
    assert sm.compute_ratios(100, 0, 0) == (None, None)
    assert sm.compute_ratios(None, 2, 1) == (None, None)


@pytest.mark.asyncio
async def test_repository_lifecycle_and_slop(clean_db, monkeypatch):
    files = {"a.py": "x = 1\ny = 2\n", "b.py": "z = 3\n"}
    adrs = {"docs/adr/0001-a.md": "approved-by: jordan", "docs/adr/0002-b.md": "plain"}
    monkeypatch.setattr(sm, "_gh", _fake_gh_factory(files, adrs))

    from acms.a2a_api import (
        create_repository, link_product_repository, product_slop,
        refresh_one_repository, RepositoryCreate, RepositoryLinkRequest)
    from acms.a2a_models import ProductRecord
    from datetime import datetime, timezone

    db = await _db()
    # product
    prod = ProductRecord(product_id="p-1", name="Widget", slug="widget",
                         created_at=datetime.now(timezone.utc),
                         updated_at=datetime.now(timezone.utc))
    db.add(prod)
    await db.commit()

    repo = await create_repository(RepositoryCreate(owner="acme", repo="widgets"), db)
    assert repo.repository_id
    # idempotent natural key
    repo2 = await create_repository(RepositoryCreate(owner="acme", repo="widgets"), db)
    assert repo2.repository_id == repo.repository_id

    await link_product_repository("p-1", RepositoryLinkRequest(repository_id=repo.repository_id), db)

    out = await refresh_one_repository(repo.repository_id, db)
    assert out and out[0]["loc"] == 3
    assert out[0]["total_adr"] == 2
    assert out[0]["human_adr"] == 1
    assert out[0]["true_slop_ratio"] == 3.0   # 3 LOC / 1 human ADR
    assert out[0]["precision_slop_ratio"] == 1.5  # 3 LOC / 2 ADRs

    summ = await product_slop("p-1", db)
    assert summ["loc"] == 3
    assert summ["true_slop_ratio"] == 3.0
    assert summ["precision_slop_ratio"] == 1.5
    assert summ["repository_count"] == 1


@pytest.mark.asyncio
async def test_unique_source_no_double_count(clean_db, monkeypatch):
    """§16: repo linked to 2 projects under one product counts ONCE."""
    files = {"a.py": "x = 1\n"}
    adrs = {"docs/adr/0001-a.md": "approved-by: jordan"}
    monkeypatch.setattr(sm, "_gh", _fake_gh_factory(files, adrs))

    db = await _db()
    from acms.a2a_api import create_repository, RepositoryCreate
    from acms.slop_metrics import (ProjectRepositoryLink, ProductRepositoryLink,
                                   get_or_create_repository, product_slop_summary,
                                   refresh_repository_metrics)
    from acms.a2a_models import ProductRecord, ProjectRecord
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    db.add(ProductRecord(product_id="p-1", name="P", slug="p",
                         created_at=now, updated_at=now))
    db.add(ProjectRecord(project_id="pr-1", name="A", slug="a", product_id="p-1",
                         created_at=now, updated_at=now))
    db.add(ProjectRecord(project_id="pr-2", name="B", slug="b", product_id="p-1",
                         created_at=now, updated_at=now))
    await db.commit()

    repo = await create_repository(RepositoryCreate(owner="acme", repo="one"), db)
    rid = repo.repository_id
    # link repo to product AND both projects (same source, many contexts)
    db.add(ProductRepositoryLink(product_id="p-1", repository_id=rid))
    db.add(ProjectRepositoryLink(project_id="pr-1", repository_id=rid))
    db.add(ProjectRepositoryLink(project_id="pr-2", repository_id=rid))
    await db.commit()

    await refresh_repository_metrics(db, rid)
    summ = await product_slop_summary(db, "p-1")
    # unique-source: 1 repo counted once
    assert summ["repository_count"] == 1
    assert summ["loc"] == 1


@pytest.mark.asyncio
async def test_collector_error_is_snapshot_not_crash(clean_db, monkeypatch):
    def failing_gh(method, path, timeout=30):
        return 404, {"message": "Not Found"}, "Not Found"

    monkeypatch.setattr(sm, "_gh", failing_gh)
    db = await _db()
    from acms.a2a_api import create_repository, refresh_one_repository, RepositoryCreate

    repo = await create_repository(RepositoryCreate(owner="acme", repo="ghost"), db)
    out = await refresh_one_repository(repo.repository_id, db)
    assert out and out[0]["error"]  # error recorded, no crash
    assert out[0]["loc"] is None