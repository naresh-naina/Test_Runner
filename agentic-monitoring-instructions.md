# Task: Add Agent-Based Test Monitoring to LocustForge

## Context
This repo (`Test_Runner`, branch `job-queue-fix`) is a FastAPI wrapper around Locust
for API load testing. Key files:
- `main.py` — API + `/ws/metrics` WebSocket streaming live metrics every 2s
- `models.py` — `TestMetrics`, `RequestStat` schemas
- `utils/runner.py` — `LocustRunner`, parses Locust CSV output into `get_metrics()`,
  maintains `timeseries` list, exposes `stop()`

Goal: build a small agentic system, using the **Google Agent Development Kit (ADK)**, that watches a
running load test and can autonomously flag anomalies, investigate them, and decide
on an incident action (e.g. stop the test). Build this **incrementally, in phases**.
Do not jump straight to the full multi-agent system — implement and verify each
phase before moving to the next.

Authentication: use Google ADK's standard Gemini API-key or Vertex AI
credentials. Do not add provider-specific credential handling to application code.

---

## Phase 1 — Single Monitoring Agent (no orchestration yet)

**Goal:** prove the basic loop works — read live metrics, have one Google ADK
agent judge them, print a verdict. No sub-agents, no tool-calling between agents yet.

1. Create a new folder `agents/` at repo root (do not touch existing app code).
2. Create `agents/monitor.py`:
   - Connects as a WebSocket client to `ws://127.0.0.1:6002/ws/metrics` (reuse the
     existing endpoint, don't duplicate metrics logic).
   - Every time a snapshot arrives, pass the relevant fields (`rps`, `avg_response_time`,
     `p95_response_time`, `total_failures`, per-endpoint `failure_rate` from `stats`)
     to a Google ADK agent query.
   - The agent's job: classify the snapshot as `OK` or `CONCERNING`, with a one-line
     reason. Keep the system prompt narrow and explicit about thresholds to reason
     about (e.g. failure rate trending up, p95 spiking, rps dropping) rather than
     giving it free rein.
   - Print each verdict to stdout with a timestamp. Don't take any action yet — this
     phase is read-only, observation only.
3. Add a short `agents/README.md` explaining how to run it (`python agents/monitor.py`
   while a test is running via the existing UI/API).
4. **Don't** poll on every single 2s tick with a full LLM call if that turns out to be
   excessive — batch or throttle (e.g. every 3rd snapshot, or only call the agent when
   values change meaningfully) so we don't burn through plan usage limits during testing.

**Acceptance for Phase 1:** I can start a test from the existing UI, run
`agents/monitor.py`, and see it print OK/CONCERNING verdicts with reasons as the test
progresses.

---

## Phase 2 — Investigation Agent (triggered handoff)

**Goal:** when Phase 1's monitor flags `CONCERNING`, hand off to a second agent that
digs deeper before anything drastic happens.

1. Create `agents/investigate.py` — a separate agent, invoked (not polling) only when
   the monitor flags something.
2. Wire it as a **tool call from the orchestrator**, not a direct function call between
   scripts — i.e. start scaffolding the orchestrator now:
   - Create `agents/orchestrator.py` — this becomes the main decision LLM.
   - Expose `monitor_snapshot` and `investigate_anomaly` as tools the orchestrator
     can call, using the Agent SDK's tool-definition pattern.
3. The investigation agent's job when invoked: pull the full current `TestMetrics`
   (via `GET /api/test/status`) and the failures list, and reason about scope —
   is this one endpoint or all endpoints, does it correlate with the spawn-rate ramp
   (early in the test) or is it a sustained degradation, etc. Return a structured
   verdict, not just prose (e.g. `{severity, scope, likely_cause, recommendation}`).
4. The orchestrator decides, based on the investigation agent's structured output,
   whether to stop here (log and continue watching) or escalate to Phase 3.

**Acceptance for Phase 2:** Orchestrator runs, calls the monitor tool in a loop,
and when a concerning snapshot appears, autonomously invokes the investigation tool
and prints its structured findings — without me manually triggering it.

---

## Phase 3 — Incident Agent (action-taking)

**Goal:** give the orchestrator an agent that can actually act on the system, gated
by the investigation agent's severity rating.

1. Create `agents/incident.py`, exposed as a third tool: `handle_incident`.
2. Give it exactly two real actions to start, both using existing endpoints —
   don't invent new ones yet:
   - Call `POST /api/test/stop` (maps to `job_queue.stop_current()`) if severity is
     high.
   - Write a structured incident log (timestamp, snapshot data, investigation
     findings, action taken) to `agents/incident_log/` as a JSON file — always,
     regardless of severity, so we have a trail.
3. The orchestrator should only call `handle_incident` after `investigate_anomaly`
   has run — never let the top-level monitoring verdict trigger an action directly.
   This three-step chain (flag → investigate → act) is the core of the system;
   keep it explicit in the orchestrator's system prompt.

**Acceptance for Phase 3:** Start a load test against a target that will produce
real failures (or artificially lower a timeout to force some), run the orchestrator,
and watch it autonomously flag → investigate → stop the test → write an incident
log, with no manual intervention after starting it.

---

## General constraints for all phases

- Keep each agent's system prompt narrow and task-specific — don't give any agent
  more tools or scope than its stated job needs.
- Log every LLM call's input/output to a local file during development so the
  decision trail is inspectable — this matters more than making it fast.
- Favor structured (JSON) outputs from sub-agents over free text, since the
  orchestrator needs to parse and act on them reliably.
- Do not modify `main.py`, `models.py`, or `utils/runner.py` unless a phase
  explicitly requires it — treat the existing app as a stable API surface the
  agents observe and call, not something to refactor.
- Ask me before moving from one phase to the next.
