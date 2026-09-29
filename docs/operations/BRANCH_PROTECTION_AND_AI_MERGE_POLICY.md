# Branch Protection and AI Merge Policy

**Status:** Active — authorized by Jordan (2026-09-29 execution plan §1.2, Phase A)
**Scope:** All `startupteams` repositories actively modified by autonomous AI agents.

---

## 1. Identity roles

| Identity | GitHub account | Role | Direct main push | Bypass protection |
|---|---|---|---|---|
| Human administrator | `jordatech` | Owner/admin | Yes (emergency) | Yes (`enforce_admins=false`) |
| Human reviewers | `robinmahz`, `birajsapkotaaa`, `Susmeetaa`, `utkarshstartupteams-crypto` | Org members | No | No |
| AI agents (incl. STEA-004) | `jordatech` token via `gh`/git | PR authors | **No** | No |
| CI | GitHub Actions app (app_id 15368) | Check reporting | No | n/a |
| Deployment identity | `release.sh` on CT122 / VM114 deploy jobs | Reads main tip, never pushes | No | n/a |

Note: AI agents authenticate with the `jordatech` token, which has admin permission. Protection
therefore relies on the **workflow discipline** in §3 (agents always work through PRs) plus the
org-level rule that only the human administrator may use direct-push authority. `enforce_admins=false`
is intentional: the human admin retains emergency direct-main capability per plan §1.2.

## 2. Protected branches (applied 2026-09-29)

| Repository | Branch | Required checks | PR required | Force-push | Delete |
|---|---|---|---|---|---|
| acms-project-framework | `main` | `syntax`, `tests`, `secret-scan` | Yes (0 approvals) | blocked | blocked |
| llm-manager-project-framework | `main` | 6 checks (syntax/tests/secret-scan/config/db/integration-smoke) — pre-existing since 2026-09-27 | Yes (0 approvals) | blocked | blocked |
| pdu-marion-ia-usa-project-framework | `main` | `test` | Yes (0 approvals) | blocked | blocked |
| hermes-base-setup | `main` | (none yet — no CI in repo) | Yes (0 approvals) | blocked | blocked |
| agentifyme-speed-to-lead | `AGENT_STEA003_AI_ML_SOFTWARE_ENGINEER` (default; no `main` exists) | — | **NOT APPLIED** | — | — |

### Known gap: agentifyme-speed-to-lead

This is the only **private** repository in scope. GitHub branch protection on private repos requires a
paid plan (API returns 403 "Upgrade to GitHub Pro or make this repository public"). Options for the
human administrator:

1. Upgrade the org/account billing plan, then apply the same policy;
2. Rely on the STEA-003 working agreement (PR-only) until then.

Tracked as an open item in the 2026-09-29 handoff.

## 3. Required workflow for AI agents

```text
branch (from current origin/main)
→ implementation
→ tests
→ push branch
→ open PR
→ required checks must pass
→ automated/self review
→ merge when green (merge commit)
→ safe deployment transaction
```

Direct pushes by AI agent identities to protected branches are **prohibited**.

## 4. Human administrator emergency path

`jordatech` (admin) may push directly to a protected branch without checks (e.g. reverting a bad
merge during an incident). This is verified working: commit `4dafe96` was pushed directly during
verification (then reverted via `74e2b2b`, also direct — that revert was an emergency-path exercise).

Human administrators also retain force-push/delete capability in a break-glass scenario
(`enforce_admins=false`; force-push and deletion are blocked at the policy layer but the admin API
can temporarily alter protection).

## 5. Verification record (2026-09-29)

- PR #35 (CI workflow) merged green: syntax + tests + secret-scan all pass.
- PR path works end-to-end with 0 human approvals required.
- Direct push as admin identity: allowed (emergency path, exercised and reverted in the same window).
- `enforce_admins=false`, `allow_force_pushes=false`, `allow_deletions=false` on all covered repos.
