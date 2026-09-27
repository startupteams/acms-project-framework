# Harness Control Mapping (ACMS-REQ-031/033/036; combined slice 3+4 plan §13)

**Rule (ADR-0010):** no mapping is declared supported without a live test
against the actual installed harness version. Unsupported controls are
advertised unsupported — never faked (never fake `pause` with `interrupt`).

**Target harness for this slice:** Hermes (MARION-IA-USA internal agent).
Installed version + commit recorded below after live verification (plan §14).

## Capability matrix — Hermes

| ACMS capability | Required semantics | Harness mapping | Tested version | Result |
|---|---|---|---|---|
| status | full current status | (pending live test — research input: upstream `/status` reports session id, title, model/provider, created/last-activity, token count, `agent_running`) | TBD | UNTESTED |
| send_work | dispatch controlled work with ACMS keys + title | (pending) | TBD | UNTESTED |
| steer | inject direction into active task/session | (pending — research input: upstream steering exists; verify on installed build) | TBD | UNTESTED |
| pause | resumable suspension of same task/session | (pending — research input: upstream `/platform pause` is platform-adapter level, NOT agent execution; likely UNSUPPORTED) | TBD | UNTESTED |
| resume | resume same paused unit | (pending) | TBD | UNTESTED |
| interrupt | stop run/turn, preserve session | (pending — research input: upstream `/stop` interrupts while preserving session) | TBD | UNTESTED |
| cancel | terminal A2A task cancellation | (pending) | TBD | UNTESTED |
| request_handoff | request Markdown handoff | (pending) | TBD | UNTESTED |
| set_session_title | set work-linked title | (pending — research input: `/new <title>` rotates session with title; title-on-dispatch path TBD) | TBD | UNTESTED |

## Lifecycle notes from planning research (inputs only, unverified)

- `/status` — centralized around session id, title, model/provider,
  created/last activity, token count, `agent_running`.
- `/stop` — interrupts the running agent turn while preserving the session
  (candidate mapping for `interrupt`).
- `/new [<title>]` — rotates session; candidate for `cancel`+fresh-session
  semantics and for title application.
- Context telemetry — upstream has explicit context-overflow /
  payload-too-large classifications; verify what the installed build exposes
  via status or API (`/usage`, gateway logs).
- Upstream tool-search defers MCP tools by context threshold (relevant prior
  finding: custom-provider context-length overrides can be ignored upstream).

## Live test log (plan §14 — fill during Phase 4)

| # | Test | Version/commit | Result | Notes |
|---|---|---|---|---|
| 1 | Record exact version/commit | TBD | — | |
| 2 | Capture status before execution | TBD | — | |
| 3 | Dispatch ACMS-keyed controlled task | TBD | — | |
| 4 | Verify title + session id | TBD | — | |
| 5 | Verify steering | TBD | — | |
| 6 | Stop/interrupt; confirm session continues | TBD | — | |
| 7 | True Pause/Resume research + test | TBD | — | |
| 8 | `/new` behavior (new session) | TBD | — | |
| 9 | Terminal cancel mapping | TBD | — | |
| 10 | Context telemetry sources | TBD | — | |
