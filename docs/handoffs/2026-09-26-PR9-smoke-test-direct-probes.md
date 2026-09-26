# PR 9 — smoke-test.sh: in-container direct probes, real exit code, Work UI check

**Date:** 2026-09-26
**Branch:** `fix/ACMS-smoke-test-direct-probes`
**Base:** `main` @ `363cf3c` (post-PR-8)
**Requirement trace:** plan §11 (post-deploy validation), §31 handoff format

## 0. Context

Drill 3 — the first end-to-end `release.sh` run with all drill-2 fixes merged —
**ACCEPTED cleanly (exit 0, validation 18/18)**. This PR fixes defects found in the
standalone operator smoke test immediately afterwards (not a release gate:
`validate-release.sh` uses the correct probe path, which is why ACCEPT was legitimate).

## 1. What changed (deploy/smoke-test.sh only, +44/−7)

1. **Direct-backend probes moved in-container.** The old script curled
   `http://127.0.0.1:8000/...` from the VM host. The app container publishes no host
   ports by design (only nginx ingress 80/443), so those two checks could never pass.
   New `probe()` runs `compose exec -T acms-app python` + urllib (the image ships no
   curl) and asserts exact status codes: `/health` 200, `/version` 200, unauth
   `/api/v1/agents` 401 (401 confirmed live as ground truth).
2. **Real exit code.** `FAILURES` was counted but the script always exited 0 —
   failures could never gate anything. Now: exit 0 iff zero failures.
3. **Work UI check added** (Slice 1 is live): unauth `GET /ui/work` via proxy must
   return 303 (login redirect), consistent with the other unauth-redirect check.

## 2. Validation

- `bash -n` clean; `git diff` verified write integrity (channel-corruption guard).
- No app code touched → 52/52 test suite state unchanged; no schema change.
- Live run **will be executed on CT122 after merge** (script currently on box is the
  buggy one; running the fixed script requires the merge → this is the only path).
- Drone: `probe()` heredoc is single-quoted (`<<'PY'`), no host-side expansion inside
  the container payload; `probe_ok` string-compares status exactly.

## 3. Risks

- Minimal: smoke test is not in the release.sh critical path. Worst case is a
  false-fail operator check, not a service impact.
- `compose exec` requires the acms-app service to be running; on a fully down stack
  the direct probes will fail (desired behavior — stack is not healthy).

## 4. Reviewer focus

- probe() status-code assertions match the live ground truth (200/200/401).
- No secrets touched; script prints no environment values.

## 5. Next after merge

Deploy hop (checkout-only, tooling file) + run the fixed smoke → then Feature
Slice 2 (`feat/ACMS-014-agent-detail-routines`): `/ui/agents/{agent_id}` per plan §14.
