# Agent Monitor — Sentinel

Agentic monitoring and incident filing for LocustForge test runs, built as five
cooperating agents, **one file per agent** so each is independently readable:

| File | Agent | What it is |
|---|---|---|
| `agents_monitor.py` | 1. Monitor | Rule-based gate + a narrow Claude Agent SDK query, classifying each judged moment as `OK`/`CONCERNING`, live, throughout the test. No MCP/tools. |
| `agents_orchestrator.py` | Orchestrator (routing only) | Pure control flow — decides *which* agent to invoke and in what order. Runs in two modes: **final** (always fires once when a test ends, pass or fail — the only mode that can reach Decisioning/Incident) and **mid-run** (investigate-only, throttled, triggered whenever Monitor flags a live snapshot CONCERNING — structurally cannot reach Decisioning or file an incident, since neither tool is even registered in this mode). **This is where the MCP server is created** — see "MCP usage" below. Deliberately does **not** judge whether something is escalation-worthy — that's Decisioning's job. |
| `agents_analysis.py` | 2. Analysis | A tool the orchestrator calls, itself a nested narrow LLM query. Reads the run's real per-endpoint stats and error bodies, identifies which APIs failed and the likely cause, and **categorizes each as `business_logic` / `system_fault` / `unclear`** (see "Business logic vs. genuine faults" below) — without inventing details it can't see in the data. |
| `agents_decisioning.py` | 3. Decisioning | A tool the orchestrator calls (final mode only), itself a nested narrow LLM query. Takes Analysis's categorized findings and makes the one substantive judgment in the whole pipeline: `escalate` (file an incident) or `pass` (the system behaved as designed). Separated from the Orchestrator specifically so routing and judgment are two distinct, independently-narrow calls rather than one session doing both — see its module docstring. |
| `agents_incident.py` | 4. Incident submission | A tool the orchestrator calls (only after Decisioning says `escalate`), plain deterministic HTTP (no LLM) — `POST`s the analysis to the downstream incident API and verifies it round-trips via `GET`. Refuses to run if Analysis hasn't produced a result yet, and filters out (or refuses entirely on an all-`business_logic` result) any endpoint categorized as an expected business response — both enforced in code, independent of what Decisioning said. |
| `agents_common.py` | *(not an agent)* | Shared JSONL logging + JSON-fence-stripping helpers, used by all five. |
| `monitor.py` | *(not an agent — the runtime host)* | The process you actually run: the WebSocket client against `/ws/metrics`, the dashboard (FastAPI), and the glue that invokes Agent 1, hands off to the Orchestrator, and composes the end-of-test summary. Owns no agent logic of its own — see `handle_snapshot()`, which just calls into `agents_monitor`/`agents_orchestrator`. |

Monitor never calls LocustForge's own control endpoints (no start/stop/reset).
The only external side effect anywhere in this pipeline is the Incident
submission tool's calls to the downstream incident API.

**Pipeline order (final mode):** Monitor flags the test ended → Orchestrator
routes to Analysis → Analysis categorizes findings → Orchestrator routes to
Decisioning → Decisioning judges escalate/pass → Orchestrator routes to
Incident submission only if Decisioning said escalate.

## MCP usage — where and why

This pipeline uses `claude_agent_sdk`'s **in-process MCP** mechanism
(`@tool` decorator + `create_sdk_mcp_server()`) so the Orchestrator can call
Analysis, Decisioning, and Incident-submission as real, structured tool calls
instead of just reasoning about them in free text. Every place MCP is
declared or used is marked with a `### MCP ###` comment in the source:

- **Tool declarations** — `agents_analysis.py`'s `build_analyze_failures_tool()`,
  `agents_decisioning.py`'s `build_decide_escalation_tool()`, and
  `agents_incident.py`'s `build_submit_incident_tool()` each wrap a function
  with `@tool(name, description, input_schema)`, turning it into an `SdkMcpTool`.
- **Server assembly** — `agents_orchestrator.py`'s `run_orchestrator_for_test()`
  collects the applicable tools for the mode and calls
  `create_sdk_mcp_server(name="incident_pipeline", tools=[...])`. This runs
  **in-process** (no subprocess, no network hop) — it's not a real external
  MCP server, just the SDK's mechanism for exposing Python functions as tools
  to the model.
- **Server registration + permission grant** — also in `run_orchestrator_for_test()`,
  on the `ClaudeAgentOptions`: `mcp_servers={"incident_pipeline": server}` plus
  `allowed_tools=[...]` (final mode: `analyze_failures`, `decide_escalation`,
  `submit_incident`; mid-run mode: `analyze_failures` only).

**Gotcha, found by live testing, not obvious from the SDK's own docstring
example:** `allowed_tools` entries for an in-process SDK MCP server must use
the `mcp__<server_name>__<tool_name>` form. A bare tool name (`"analyze_failures"`,
which is literally what the SDK's own example shows) is silently denied under
`permission_mode="dontAsk"` — the orchestrator doesn't error, it just reports
back that it can't proceed. This is called out at the top of
`agents_orchestrator.py`.

Agent 1 (Monitor), the Analysis subagent's nested LLM call, and the
Decisioning subagent's nested LLM call all run with `tools=[]` /
`allowed_tools=[]` — no MCP involved there at all, they're plain single-turn
classification/reasoning queries. Only the Orchestrator's own session has an
MCP server wired in.

## Business logic vs. genuine faults

A Locust "failure" just means the API returned an HTTP status >= 400 — a raw mechanical
count. Some of those are the API correctly reporting an expected outcome (e.g. "No
records available", "No balance remaining", "insufficient stock" — the system working
exactly as designed), not a fault. Analysis classifies every failing endpoint as:

- `business_logic` — a clean, well-formed application message describing a normal
  outcome, no exception class, no stack trace, no nested cause. **This is a general
  pattern, not a keyword list** — the prompt gives illustrative examples but is
  explicitly told real APIs phrase this differently everywhere, and to match the
  pattern rather than specific wording.
- `system_fault` — 5xx status, an exception class name, a stack trace / nested cause
  chain, or a connection-level failure with no body at all.
- `unclear` — not confident either way. Defaults here rather than guessing
  `business_logic`, since a missed real fault is worse than an unnecessary flag.

The escalate/pass judgment itself is made by **Decisioning** (`agents_decisioning.py`),
not the Orchestrator: when there are any failures, the final-mode Orchestrator always
calls `analyze_failures` first, then hands the categorized result to `decide_escalation`,
which escalates if at least one endpoint came back `system_fault`/`unclear` and passes if
every one is `business_logic`. The Orchestrator itself just acts on whatever
`decide_escalation` returns — it's told explicitly not to make that call itself.
`submit_incident` additionally filters `business_logic` entries out of the payload in
code (see the table above), and refuses outright if nothing genuine is left — so this
holds even if Decisioning's prompt-driven judgment ever slips.

## Prerequisite: response bodies on failure

For Analysis to report *real* root causes (exception class, message, cause
chain) instead of guessing, the generated Locust script needs to have captured
the downstream service's actual error response body, not just the HTTP status
code. `utils/script_generator.py` was updated to do this — every generated
script now records `f"Got status {code}: {body[:2000]}"` on failure, so the
response body flows through into `TestMetrics.errors`. This only affects newly
generated scripts; a test run before this change won't have body detail
available to Analysis (it still works, Analysis just falls back to a generic
description in that case).

## Why a separate virtualenv

`claude-agent-sdk` depends on `mcp`, which pulls in a newer `starlette`/`anyio`
than the main app's pinned `fastapi==0.115.12` tolerates. Installing it into the
same environment as the main app **will** break `main.py` (this happened once
while building this — recovered by reverting the shared env). `agents/.venv` is
a dedicated virtualenv so this can never collide with the main app's deps again.
Do not `pip install claude-agent-sdk` into the main project's environment.

## Prerequisites

1. **Claude Code authentication.** The Python SDK shells out to a `claude` CLI
   binary — it does not talk to the API directly and does not need
   `ANTHROPIC_API_KEY`. `pip install claude-agent-sdk` bundles its own
   `claude.exe` (`agents/.venv/Lib/site-packages/claude_agent_sdk/_bundled/`),
   so a separate CLI install usually isn't necessary. If it can't find a working
   login, it falls back to a system-wide `claude` on PATH — install one
   (`npm install -g @anthropic-ai/claude-code`) and run `claude login` in that
   case. Either way it uses your normal Claude Code / Claude subscription
   session, not a raw API key.

2. **The main app running** (`python main.py`, or `uvicorn main:app`) on its
   usual port, so `/ws/metrics` is reachable.

## Setup (one time)

```powershell
cd agents
python -m venv .venv
.venv\Scripts\pip install -r ..\requirements-agents.txt
```

(This repo already has `agents/.venv` set up if you're picking this up mid-session.)

## Run

```powershell
agents\.venv\Scripts\python.exe agents\monitor.py
```

Then start a test from the normal LocustForge UI, and open **http://127.0.0.1:6010**
in a browser to watch verdicts stream in live. Verdicts also print to stdout.

- **While the test is running**, if Monitor flags a snapshot CONCERNING, the
  Orchestrator checks in (throttled, see below) and reports an "Interim Check"
  — it can call Analysis to investigate, but it can never file an incident at
  this point.
- **When the test ends**, the Orchestrator always runs a "Final Decision" —
  pass or fail, escalated or not — so the panel never just sits blank after a
  clean run.

## How the call-throttling works

Every snapshot from `/ws/metrics` updates the dashboard's raw metrics tiles for
free (no LLM cost). A Claude call only fires when either:

- the local rule-based gate trips (endpoint failure rate > 5%, failures
  increased since the last snapshot, rps dropped >40% below the recent
  baseline past the ramp-up window, or p95 spiked >60% above baseline), or
- `HEARTBEAT_SECONDS` (default 30s) has passed since the last call, so the
  feed isn't silent even when nothing looks wrong.

Tune thresholds and intervals at the top of `agents_monitor.py`.

The Orchestrator has its own, separate throttle for mid-run checks:
`MIN_MIDRUN_INTERVAL_SECONDS` (default 60s) in `agents_orchestrator.py` — a
mid-run check is a real multi-turn Sonnet + Haiku round trip (Orchestrator +
Analysis), noticeably more expensive than Monitor's own single Haiku call, so
it's throttled harder and only triggers on an actual CONCERNING verdict, never
on a heartbeat.

## Incident cadence

One incident per test, filed only at the very end (the Orchestrator's
"final" mode, and only when Decisioning returns `escalate`), aggregating
every distinct failing API seen during the whole run — not one per API, and
never filed mid-test. Mid-run Orchestrator checks are investigate-only:
`decide_escalation` and `submit_incident` aren't even registered as callable
tools in that mode, so this is enforced structurally, not just by prompt.

## Test Summary (end-of-test UI card)

When a test ends, once the Orchestrator's final-mode run completes,
`monitor.py`'s `broadcast_test_summary()` composes a single `test_summary`
event for the dashboard's "Test Summary" card — total requests/failures,
Monitor's OK/CONCERNING/ERROR tally for the run, Analysis's findings,
Decisioning's verdict + reasoning, and the incident record if one was filed.

This is **code-composed, not an extra LLM call** — every field it uses was
already produced by an agent earlier in the same pipeline run (Monitor's
tally counters on `MonitorAgentState`, and `analysis`/`decision`/`incident`
returned from `run_orchestrator_for_test()`); the summary step just gathers
them into one payload. It only fires for the final mode — mid-run checks
never produce a `test_summary` broadcast.

## Logs

Every agent's call input/output is appended to its own file, one per agent,
via `agents_common.log_jsonl()`:

- `agents/logs/monitor_<date>.jsonl` — Agent 1 classifications
- `agents/logs/orchestrator_<date>.jsonl` — the routing outcome
- `agents/logs/analysis_<date>.jsonl` — Analysis results
- `agents/logs/decisioning_<date>.jsonl` — escalate/pass decisions
- `agents/logs/incident_<date>.jsonl` — filed incidents (or submission errors)

That directory is gitignored.

## Config (env vars, optional)

| Var | Default | Purpose |
|---|---|---|
| `MONITOR_MAIN_WS_URL` | `ws://127.0.0.1:6002/ws/metrics` | Main app's metrics WS |
| `MONITOR_UI_HOST` / `MONITOR_UI_PORT` | `127.0.0.1` / `6010` | This dashboard |
| `API_KEY` | *(from repo root `.env`)* | Only needed if the main app has `API_KEY` set |

Per-agent model choice and the incident API base aren't env vars yet — edit
the constant at the top of the relevant file: `MONITOR_MODEL` in
`agents_monitor.py`, `ANALYSIS_MODEL` in `agents_analysis.py`,
`DECISION_MODEL` in `agents_decisioning.py`, `ORCHESTRATOR_MODEL` in
`agents_orchestrator.py`, `INCIDENT_API_BASE` in `agents_incident.py`.
