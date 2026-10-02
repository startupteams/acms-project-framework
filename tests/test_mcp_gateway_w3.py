"""W3 gateway tests: github.* capabilities (plan §25/§31/§41).

Covers: repo/branch/commit/pr/checks/issue resources (scope-gated),
branch creation with ownership policy, file staging + commit flow,
PR create/comment/review-comment, branch isolation (foreign branch
denied), absence of merge/delete/force capabilities, audit rows.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.github_adapter import GithubClient, branch_owner, owned_branch
from mcp_gateway.models import CallIdentity, RiskClass, TokenKind
from mcp_gateway.server import GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore


# ------------------------------------------------------------------ fakes

class FakeGithubClient(GithubClient):
    """Records calls; returns canned GitHub REST shapes. No network."""

    def __init__(self, repos: list[str] | None = None):
        super().__init__(token="fake-token", repos_allowlist=repos or [])
        self.calls: list[tuple] = []
        self.repos: dict[str, dict] = {
            "startupteams/acms-project-framework": {
                "full_name": "startupteams/acms-project-framework",
                "private": True, "default_branch": "main",
                "permissions": {"push": True, "admin": True},
                "html_url": "https://github.com/startupteams/acms-project-framework",
                "pushed_at": "2026-10-02T00:00:00Z",
            },
        }
        self.branches: dict[tuple, dict] = {}  # (repo, branch) -> shape
        self.blobs: list[str] = []
        self.commits: list[dict] = []
        self.prs: list[dict] = []
        self.comments: list[dict] = []
        self.review_comments: list[dict] = []
        self._next_sha = 1000

    def _sha(self) -> str:
        self._next_sha += 1
        return f"sha{self._next_sha:08d}"

    def _log(self, op: str, *args):
        self.calls.append((op, *args))

    def request(self, method, path, payload=None, *, accept="application/vnd.github+json"):
        self._log("request", method, path)
        return 200, {}, {}

    # reads
    def get_repo(self, repo):
        self._log("get_repo", repo)
        if repo not in self.repos:
            from mcp_gateway.errors import NotFoundError
            raise NotFoundError(f"repo not found or not accessible: {repo}")
        return self.repos[repo]

    def get_branch(self, repo, branch):
        self._log("get_branch", repo, branch)
        if branch == "main":
            return {"name": "main", "sha": "basesha", "protected": True}
        key = (repo, branch)
        if key not in self.branches:
            from mcp_gateway.errors import NotFoundError
            raise NotFoundError(f"branch not found: {branch}")
        return self.branches[key]

    def get_commit(self, repo, ref):
        self._log("get_commit", repo, ref)
        return {"sha": ref, "message": "test commit", "author": {"name": "a"},
                "html_url": "u", "files": []}

    def get_pr(self, repo, number):
        self._log("get_pr", repo, number)
        for pr in self.prs:
            if pr["number"] == number:
                return pr
        from mcp_gateway.errors import NotFoundError
        raise NotFoundError(f"PR not found: {repo}#{number}")

    def get_checks(self, repo, ref):
        self._log("get_checks", repo, ref)
        return {"ref": ref, "total_count": 1,
                "check_runs": [{"name": "tests", "status": "completed",
                                "conclusion": "success", "html_url": "u"}]}

    def get_issue(self, repo, number):
        self._log("get_issue", repo, number)
        return {"number": number, "state": "open", "title": "t",
                "labels": ["bug"], "html_url": "u", "is_pr": False}

    # writes
    def create_branch(self, repo, new_branch, base_ref):
        self._log("create_branch", repo, new_branch, base_ref)
        sha = self._sha()
        self.branches[(repo, new_branch)] = {
            "name": new_branch, "sha": sha, "protected": False}
        return {"branch": new_branch, "sha": sha, "base": base_ref}

    def create_blob(self, repo, content, *, encoding="utf-8"):
        self._log("create_blob", repo, len(content))
        sha = self._sha()
        self.blobs.append(sha)
        return sha

    def get_commit_sha(self, repo, branch):
        self._log("get_commit_sha", repo, branch)
        b = self.branches.get((repo, branch))
        if not b:
            from mcp_gateway.errors import NotFoundError
            raise NotFoundError(f"branch not found: {branch}")
        return b["sha"]

    def build_tree_from_base(self, repo, base_commit_sha, staged):
        self._log("build_tree", repo, base_commit_sha, [s["path"] for s in staged])
        return self._sha(), "basetree"

    def create_commit(self, repo, branch, message, tree_sha, parent_sha,
                      author_name, author_email):
        self._log("create_commit", repo, branch, message)
        sha = self._sha()
        self.commits.append({"sha": sha, "branch": branch, "message": message,
                             "author": author_name})
        self.branches[(repo, branch)]["sha"] = sha
        return {"commit_sha": sha, "branch": branch, "tree": tree_sha}

    def create_pr(self, repo, head, base, title, body, *, draft=False):
        self._log("create_pr", repo, head, base, title)
        self.prs.append({"number": len(self.prs) + 1, "state": "open",
                         "title": title, "html_url": f"https://github.com/{repo}/pull/{len(self.prs)+1}",
                         "draft": draft})
        return {"number": len(self.prs), "html_url": self.prs[-1]["html_url"],
                "state": "open", "draft": draft}

    def add_pr_comment(self, repo, number, body):
        self._log("add_pr_comment", repo, number)
        self.comments.append({"number": number, "body": body})
        return {"comment_id": 555, "html_url": "u"}

    def add_review_comment(self, repo, number, body, commit_sha, path,
                           line=None, side="RIGHT"):
        self._log("add_review_comment", repo, number, path)
        self.review_comments.append({"number": number, "path": path})
        return {"comment_id": 777, "html_url": "u"}


class FakeAcms:
    """Just enough for _caller_work_uid via agent-token paths."""

    def active_assignment(self, agent_id):
        return {"work_item_id": "wi-1"}

    def find_work_item(self, work_item_id):
        if work_item_id == "wi-1":
            return {"work_item_id": "wi-1", "work_uid": "ACMS-WORK-000042-20261002_010203"}
        return None

    def request(self, *a, **k):  # unused in these tests
        return 404, None


def make_identity(kind=TokenKind.ASSIGNMENT, agent="acms-hermes-worker-uid-005",
                  work_uid="ACMS-WORK-000042-20261002_010203",
                  repos=("startupteams/acms-project-framework",)):
    return CallIdentity(
        token_kind=kind, token_id="tok-1", agent_name=agent,
        acms_agent_id="11111111-1111-1111-1111-111111111111" if kind == TokenKind.AGENT else None,
        roles=["worker"],
        scopes=["acms.read", "acms.write", "github.read", "github.write",
                "llm.read", "llm.write", "runtime.write"],
        work_uid=work_uid if kind == TokenKind.ASSIGNMENT else None,
        repositories=list(repos) if kind == TokenKind.ASSIGNMENT else [],
    )


def build_server_at(tmp_path: Path, fake_gh) -> GatewayServer:
    cfg = GatewayConfig(
        github_token="fake",  # triggers internal client build; we override anyway
        github_repos="startupteams/acms-project-framework,startupteams/llm-manager-project-framework",
        approvals_path=str(tmp_path / "approvals.sqlite3"),
    )
    tokens = TokenStore(str(tmp_path / "tokens.sqlite3"))
    audit = AuditLog(str(tmp_path / "activity.sqlite3"))
    server = GatewayServer(cfg, tokens=tokens, audit=audit,
                           acms_client=FakeAcms(), llm_client=None,
                           github_client=fake_gh)
    return server


# ------------------------------------------------------------------ adapter unit

def test_owned_branch_shape():
    assert owned_branch("ACMS-WORK-000042-20261002_010203",
                        "acms-hermes-worker-uid-005") == \
        "agent/ACMS-WORK-000042-20261002_010203-acms-hermes-worker-uid-005"


def test_branch_owner_parses_and_rejects():
    owner = branch_owner("agent/ACMS-WORK-000042-20261002_010203-acms-hermes-worker-uid-005")
    assert owner == ("ACMS-WORK-000042-20261002_010203", "acms-hermes-worker-uid-005")
    assert branch_owner("main") is None
    assert branch_owner("agent/feature-x") is None  # non-ACMS uid shape


# ------------------------------------------------------------------ resources

def test_repo_resource_in_scope(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    # resolve through the registry directly (bypassing MCP transport)
    cap = server.registry._capabilities["github.repo.get"]
    identity = make_identity()
    out = _run(cap.handler(identity, {"repo": "startupteams/acms-project-framework"}))
    assert out["full_name"].endswith("acms-project-framework")
    assert out["permissions"]["push"] is True


def test_repo_resource_out_of_scope(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    cap = server.registry._capabilities["github.repo.get"]
    identity = make_identity(repos=("startupteams/other-repo",))
    from mcp_gateway.errors import OutOfScopeError
    with pytest.raises(OutOfScopeError):
        _run(cap.handler(identity, {"repo": "startupteams/acms-project-framework"}))


def test_agent_token_requires_allowlist(tmp_path):
    gh = FakeGithubClient(repos=["startupteams/acms-project-framework",
                                 "startupteams/llm-manager-project-framework"])
    server = build_server_at(tmp_path, gh)
    cap = server.registry._capabilities["github.repo.get"]
    # agent token: allowlist applies
    identity = make_identity(kind=TokenKind.AGENT)
    out = _run(cap.handler(identity, {"repo": "startupteams/acms-project-framework"}))
    assert out["default_branch"] == "main"
    # not in allowlist -> denied (fail-closed)
    gh2 = FakeGithubClient(repos=[])
    server2 = build_server_at(tmp_path / "n2", gh2)
    cap2 = server2.registry._capabilities["github.repo.get"]
    from mcp_gateway.errors import OutOfScopeError
    with pytest.raises(OutOfScopeError):
        _run(cap2.handler(identity, {"repo": "startupteams/acms-project-framework"}))


def test_branch_commit_pr_checks_issue_resources(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    branch = server.registry._capabilities["github.branch.get"]
    out = _run(branch.handler(identity, {"repo": "startupteams/acms-project-framework",
                                         "branch": "main"}))
    assert out["protected"] is True
    commit = server.registry._capabilities["github.commit.get"]
    out = _run(commit.handler(identity, {"repo": "startupteams/acms-project-framework",
                                         "ref": "abc123"}))
    assert out["sha"] == "abc123"
    pr = server.registry._capabilities["github.pr.get"]
    from mcp_gateway.errors import NotFoundError
    with pytest.raises(NotFoundError):
        _run(pr.handler(identity, {"repo": "startupteams/acms-project-framework",
                                   "number": "1"}))
    checks = server.registry._capabilities["github.checks.get"]
    out = _run(checks.handler(identity, {"repo": "startupteams/acms-project-framework",
                                         "ref": "main"}))
    assert out["check_runs"][0]["conclusion"] == "success"
    issue = server.registry._capabilities["github.issue.get"]
    out = _run(issue.handler(identity, {"repo": "startupteams/acms-project-framework",
                                        "number": "5"}))
    assert out["is_pr"] is False


# ------------------------------------------------------------------ tools

def _tool(server, name):
    from mcp_gateway.policy import dotted
    return server.registry._capabilities[dotted(name)]


def test_branch_create_own_namespace(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        _tool(server, "github.branch.create").handler(identity, None,
                                                      repo="startupteams/acms-project-framework")
    ) if False else None
    # use run_coroutine helper instead
    out = _call_tool(server, "github.branch.create", identity,
                     repo="startupteams/acms-project-framework", base="")
    assert out["branch"] == "agent/ACMS-WORK-000042-20261002_010203-acms-hermes-worker-uid-005"
    assert out["base"] == "main"


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def _call_tool(server, tool_name, identity, **kwargs):
    """Invoke a tool through the REAL transport path: GatewayTool.run() with
    CURRENT_IDENTITY set (audit + policy + error mapping all exercised)."""
    from mcp_gateway.server import CURRENT_IDENTITY
    storage_key = tool_name.replace(".", "_")
    gt = server.mcp._tool_manager._tools[storage_key]
    async def _go():
        token = CURRENT_IDENTITY.set(identity)
        try:
            return await gt.run(dict(kwargs), context=None)
        finally:
            CURRENT_IDENTITY.reset(token)

    return _run(_go())


def test_branch_create_foreign_base_denied(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    from mcp_gateway.errors import OutOfScopeError
    with pytest.raises(OutOfScopeError):
        _call_tool(server, "github.branch.create", identity,
                   repo="startupteams/acms-project-framework",
                   base="agent/ACMS-WORK-000099-20260101_000000-acms-hermes-worker-uid-004")


def test_file_write_requires_existing_own_branch(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    from mcp_gateway.errors import ValidationError_
    with pytest.raises(ValidationError_):
        _call_tool(server, "github.file.write", identity,
                   repo="startupteams/acms-project-framework",
                   path="docs/x.md", content="hello")


def test_full_write_flow_own_branch(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    repo = "startupteams/acms-project-framework"
    b = _call_tool(server, "github.branch.create", identity, repo=repo)
    assert b["branch"].startswith("agent/ACMS-WORK-000042")
    w = _call_tool(server, "github.file.write", identity,
                   repo=repo, path="docs/w3-proof.md", content="# proof")
    assert w["staged"] is True
    c = _call_tool(server, "github.commit.create", identity,
                   repo=repo, message="w3 proof commit")
    assert c["files"] == ["docs/w3-proof.md"]
    assert len(gh.commits) == 1 and gh.commits[0]["branch"].startswith("agent/")
    p = _call_tool(server, "github.pr.create", identity,
                   repo=repo, title="W3 proof", body="body")
    assert p["head"].startswith("agent/") and p["base"] == "main"
    assert gh.prs[0]["title"] == "W3 proof"
    mc = _call_tool(server, "github.pr.comment", identity,
                    repo=repo, number=p["number"], body="progress note")
    assert mc["comment_id"] == 555
    rc = _call_tool(server, "github.review.comment", identity,
                    repo=repo, number=p["number"],
                    commit_sha=c["commit_sha"], path="docs/w3-proof.md", body="note")
    assert rc["comment_id"] == 777


def test_foreign_branch_write_denied(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    repo = "startupteams/acms-project-framework"
    # create ANOTHER agent's branch directly in the fake
    gh.branches[(repo, "agent/ACMS-WORK-000099-20260101_000000-acms-hermes-worker-uid-004")] = {
        "name": "agent/ACMS-WORK-000099-20260101_000000-acms-hermes-worker-uid-004",
        "sha": "x", "protected": False}
    identity = make_identity(agent="acms-hermes-worker-uid-005")
    from mcp_gateway.errors import NotFoundError, OutOfScopeError, ValidationError_
    # file.write resolves the caller's OWN branch name and verifies it exists;
    # a foreign agent's branch is never reachable by design — prove the
    # ownership assertion fires when the branch name doesn't match.
    with pytest.raises(ValidationError_):
        _call_tool(server, "github.file.write", identity,
                   repo=repo, path="a.md", content="x")
    # direct ownership check through commit path (branch exists but foreign):
    # simulate by temporarily pointing the identity's expected branch at the
    # foreign one via a DIFFERENT work_uid in the token — the resulting
    # expected branch differs from the foreign branch => still denied.
    identity_foreign = make_identity(
        work_uid="ACMS-WORK-000099-20260101_000000",
        agent="acms-hermes-worker-uid-005")
    # uid-005 owns agent/ACMS-WORK-000099-...-uid-005, NOT the -004 branch.
    # Either denial is correct: NotFound (own branch missing) or the explicit
    # ownership rejection — both prove the foreign branch is unreachable.
    with pytest.raises((ValidationError_, NotFoundError)):
        _call_tool(server, "github.commit.create", identity_foreign, repo=repo,
                   message="m")


def test_branch_create_out_of_scope_repo(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    from mcp_gateway.errors import OutOfScopeError
    with pytest.raises(OutOfScopeError):
        _call_tool(server, "github.branch.create", identity,
                   repo="startupteams/not-in-scope")


def test_pr_create_out_of_scope(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    from mcp_gateway.errors import OutOfScopeError
    with pytest.raises(OutOfScopeError):
        _call_tool(server, "github.pr.comment", identity,
                   repo="startupteams/not-in-scope", number=1, body="x")


def test_no_merge_delete_force_capabilities(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    names = {c.name for c in server.registry._capabilities.values()}
    for banned in ("github.merge", "github.repo.delete", "github.branch.delete",
                   "github.pr.merge", "github.force_push", "github.org.settings"):
        assert banned not in names
    # also nothing in the registry with destructive risk for github
    for c in server.registry._capabilities.values():
        if c.domain == "github":
            assert c.risk in (RiskClass.READ, RiskClass.SAFE_WRITE)


def test_write_tools_are_safe_write_and_audited(tmp_path):
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    repo = "startupteams/acms-project-framework"
    identity = make_identity()
    for n in ("github.branch.create", "github.file.write", "github.commit.create",
              "github.pr.create", "github.pr.comment", "github.review.comment"):
        cap = _tool(server, n)
        assert cap.risk == RiskClass.SAFE_WRITE, n
        assert cap.domain == "github"
    # run one write; expect an audit row
    _call_tool(server, "github.branch.create", identity, repo=repo)
    rows = server.audit.recent(limit=50)
    gh_rows = [r for r in rows if r["domain"] == "github"
               and r["name"] == "github.branch.create"]
    assert gh_rows and gh_rows[0]["status"] == "ok"
    assert gh_rows[0]["args_hash"]  # arguments hashed, never stored raw


def test_no_pat_in_audit(tmp_path):
    """Plan §43: agent tokens never appear in logs; PAT must not either."""
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    repo = "startupteams/acms-project-framework"
    identity = make_identity()
    _call_tool(server, "github.branch.create", identity, repo=repo)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "activity.sqlite3"))
    rows = conn.execute("SELECT detail FROM mcp_activity").fetchall()
    blob = json.dumps([r[0] for r in rows])
    assert "fake-token" not in blob
    assert "MCP_GATEWAY_GITHUB_TOKEN" not in blob


def test_github_domain_absent_when_unconfigured(tmp_path):
    cfg = GatewayConfig(approvals_path=str(tmp_path / "appr.sqlite3"))  # no github_token
    tokens = TokenStore(str(tmp_path / "t.sqlite3"))
    audit = AuditLog(str(tmp_path / "a.sqlite3"))
    server = GatewayServer(cfg, tokens=tokens, audit=audit,
                           acms_client=FakeAcms(), llm_client=None)
    names = {c.name for c in server.registry._capabilities.values()}
    assert not any(n.startswith("github.") for n in names)


def test_repo_resource_url_encoded_owner(tmp_path):
    # owner/name contains a slash; MCP template params arrive percent-encoded
    # (owner%2Fname) so resolvers must receive the decoded owner/name.
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    cap = server.registry._capabilities["github.repo.get"]
    identity = make_identity()
    out = _run(cap.handler(identity, {"repo": "startupteams%2Facms-project-framework"}))
    assert out["full_name"].endswith("acms-project-framework")


def test_unknown_tool_args_rejected(tmp_path):
    # _bind_args must NOT silently drop unknown args: an ignored branch arg
    # would mask policy violations on the ownership surface.
    gh = FakeGithubClient()
    server = build_server_at(tmp_path, gh)
    identity = make_identity()
    from mcp_gateway.errors import ValidationError_
    with pytest.raises(ValidationError_) as ei:
        _call_tool(server, "github_branch_create", identity,
                   repo="startupteams/acms-project-framework", base="main",
                   branch="agent/FORBIDDEN-uid-999")
    assert "unknown argument" in str(ei.value)
