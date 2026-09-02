"""
Orchestrator — pure routing/control, no judgment of its own.

Runs in two distinct modes:

  - **Mid-run (investigate-only)**: triggered from monitor.py whenever Agent 1
    (Monitor) flags a live snapshot as CONCERNING, throttled by
    MIN_MIDRUN_INTERVAL_SECONDS. Only the Analysis tool is available in this
    mode — submit_incident and decide_escalation are not even registered, so
    it is *structurally* impossible to file an incident mid-run, not just
    discouraged by prompt. This mode exists to surface an early root-cause
    read while the test is still running; it never decides anything or acts.
  - **Final**: triggered once, always, when a test ends — regardless of
    whether it passed or failed. This is the only mode with decide_escalation
    and submit_incident available. On a clean pass it still runs and reports
    a real "no incident needed" outcome rather than staying silent.

The Orchestrator's own job is deliberately mechanical: call Analysis, then
(final mode only) hand its result to Decisioning, then act on whatever
Decisioning decided. The actual judgment — is this escalation-worthy — lives
in agents_decisioning.py, not here. See that module's docstring for why this
is split out rather than folded into the Orchestrator's own reasoning.

### MCP ###
This is the one place an in-process MCP *server* gets created
(claude_agent_sdk.create_sdk_mcp_server) and wired into a ClaudeAgentOptions
session (mcp_servers=...). The tools it serves are DECLARED in
agents_analysis.py, agents_decisioning.py, and agents_incident.py (via the
@tool decorator in each) — this file only assembles them into one server (a
*different* tool set depending on is_final — see run_orchestrator_for_test)
and grants this session permission to call them.

Gotcha found by live testing (not obvious from the SDK's own docstring
example, which uses bare tool names): allowed_tools entries for an in-process
SDK MCP server must use the "mcp__<server_name>__<tool_name>" form. A bare
tool name is silently denied under permission_mode="dontAsk" — the
orchestrator just reports it can't proceed instead of erroring loudly.

Every step is reported through on_step(agent, status, detail) so the caller
(monitor.py) can broadcast it to the dashboard in real time.
"""

import json
import logging
import time
from typing import Awaitable, Callable, Optional

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query

import agents_analysis
import agents_decisioning
import agents_incident
from agents_common import log_jsonl

logger = logging.getLogger("agents.orchestrator")

ORCHESTRATOR_MODEL = "claude-sonnet-5"

# Throttle for mid-run investigate calls — these are triggered by Agent 1
# flagging CONCERNING, which can happen frequently; each mid-run call is a
# real multi-turn Sonnet + Haiku round trip, so this keeps cost/latency sane.
MIN_MIDRUN_INTERVAL_SECONDS = 60

OnStep = Callable[[str, str, dict], Awaitable[None]]

ORCHESTRATOR_FINAL_SYSTEM_PROMPT = """You are the orchestrator for a load-testing pipeline. Your
job is routing — deciding which agent to invoke and in what order — not judgment. A test run has
just finished. You are given a summary of per-endpoint failure rates and response times for the
whole run — this may show zero failures (a clean pass) or some failures.

Follow this procedure exactly:
- If total_failures is 0: the run was clean. State that in one sentence. Do not call any tool.
- If total_failures > 0:
  1. Call the analyze_failures tool (no arguments) to get a categorized, root-cause read on what
     is actually happening.
  2. Call the decide_escalation tool (no arguments) to have the decisioning step judge, from that
     categorized read, whether this warrants an incident. You must call analyze_failures before
     this — it will refuse otherwise. Do not make the escalate/pass judgment yourself; that
     decision belongs to decide_escalation, not you.
  3. If decide_escalation returned "escalate", call submit_incident, passing test_time as an
     ISO-8601 UTC timestamp for when this test ran, to file the incident.
  4. If decide_escalation returned "pass", do not call submit_incident.
  5. Report back in one or two sentences what was decided (citing decide_escalation's reasoning)
     and the resulting incident number if one was filed.
"""

ORCHESTRATOR_MIDRUN_SYSTEM_PROMPT = """You are the central decisioning agent for a load-testing
pipeline, checking in on a test that is STILL RUNNING (it has not finished yet). Agent 1 (the
Monitor) just flagged the current moment as CONCERNING, based on raw failure-rate/response-time
thresholds. You are given a live, partial summary of per-endpoint failure rates and response times
collected so far — not the final picture.

Important: those raw thresholds cannot tell a genuine system fault from the API correctly
reporting an expected business-logic outcome (e.g. "No records available", "No balance
remaining") — the system working exactly as designed. Do not assume this is a real problem yet.

Your job right now is to investigate only, not to act: call the analyze_failures tool (no
arguments) to get a categorized, root-cause read on what is happening right now, then report it in
one or two sentences. If the categorized results show only "business_logic" findings, say so
plainly — that's a completely normal outcome, not a failure of this check, not something to sound
alarmed about. You do NOT have a submit_incident tool available on purpose — no incident is ever
filed mid-run regardless of what you find. A single, final decision (and incident, if warranted) is
made once, after the test completes, using the complete picture.
"""


class OrchestratorRunState:
    """Per-invocation state the Analysis, Decisioning, and Incident-submission
    tools close over: this run's current metrics (final, or a partial mid-run
    snapshot), the analysis result once produced (also the enforcement point
    stopping decide_escalation/submit_incident from firing without it), the
    decisioning result once produced, and the resulting incident record."""

    def __init__(self, final_metrics: dict, on_step: OnStep):
        self.final_metrics = final_metrics
        self.on_step = on_step
        self.analysis_result: Optional[list[dict]] = None
        self.decision_result: Optional[dict] = None
        self.incident_record: Optional[dict] = None


class OrchestratorHostState:
    """Host-side throttle state for scheduling mid-run Orchestrator
    invocations — analogous to agents_monitor.MonitorAgentState, but for
    deciding *when* the host (monitor.py) should trigger an interim check.
    The final (end-of-test) invocation ignores this entirely — it always
    runs, unthrottled."""

    def __init__(self):
        self.last_midrun_call_ts: float = 0.0
        self.midrun_in_flight: bool = False


def should_invoke_midrun(state: OrchestratorHostState) -> bool:
    if state.midrun_in_flight:
        return False
    return (time.time() - state.last_midrun_call_ts) >= MIN_MIDRUN_INTERVAL_SECONDS


async def run_orchestrator_for_test(metrics: dict, on_step: OnStep, is_final: bool = True) -> dict:
    """Run the central decisioning agent once.

    is_final=True (test just ended, always called regardless of pass/fail):
        full decision, both tools available, can file an incident.
    is_final=False (mid-run, triggered by a CONCERNING flag from Monitor):
        investigate-only — only analyze_failures is registered, so
        submit_incident cannot be called even if the model tried.

    Returns {"escalated": bool, "final_text": str, "incident": dict | None}.
    """
    state = OrchestratorRunState(metrics, on_step)

    # ### MCP: assemble this run's in-process server ###
    # A fresh server (and fresh tool closures over a fresh `state`) per
    # invocation — cheap, and keeps one run's state from ever leaking into
    # another's. The tool set itself differs by mode: decide_escalation and
    # submit_incident are simply not built/registered for a mid-run call, so
    # it's not just prompted against — they structurally do not exist as
    # callable tools.
    if is_final:
        tools = [
            agents_analysis.build_analyze_failures_tool(state),
            agents_decisioning.build_decide_escalation_tool(state),
            agents_incident.build_submit_incident_tool(state),
        ]
        allowed_tools = [
            "mcp__incident_pipeline__analyze_failures",
            "mcp__incident_pipeline__decide_escalation",
            "mcp__incident_pipeline__submit_incident",
        ]
        system_prompt = ORCHESTRATOR_FINAL_SYSTEM_PROMPT
    else:
        tools = [agents_analysis.build_analyze_failures_tool(state)]
        allowed_tools = ["mcp__incident_pipeline__analyze_failures"]
        system_prompt = ORCHESTRATOR_MIDRUN_SYSTEM_PROMPT

    server = create_sdk_mcp_server(name="incident_pipeline", tools=tools)

    stats = metrics.get("stats") or []
    summary = {
        "total_requests": metrics.get("total_requests"),
        "total_failures": metrics.get("total_failures"),
        "elapsed": metrics.get("elapsed"),
        "endpoints": [
            {
                "name": s.get("name"),
                "method": s.get("method"),
                "num_requests": s.get("num_requests"),
                "num_failures": s.get("num_failures"),
                "failure_rate": round(s.get("failure_rate", 0), 2),
                "avg_response_time": round(s.get("avg_response_time", 0), 1),
            }
            for s in stats
        ],
    }

    options = ClaudeAgentOptions(
        model=ORCHESTRATOR_MODEL,
        system_prompt=system_prompt,
        permission_mode="dontAsk",
        # ### MCP: register the server, and explicitly allow only its
        # applicable tool(s) for this mode ###
        # See the module docstring for the mcp__<server>__<tool> naming gotcha.
        mcp_servers={"incident_pipeline": server},
        allowed_tools=allowed_tools,
        max_turns=6,
    )

    note = "Reviewing test outcome..." if is_final else "Checking in on a CONCERNING flag from Monitor..."
    await state.on_step("orchestrator", "active", {"note": note, "summary": summary, "final": is_final})

    final_text = ""
    try:
        async for message in query(prompt=json.dumps(summary), options=options):
            if isinstance(message, ResultMessage):
                final_text = message.result or ""
    except Exception as e:
        logger.error(f"orchestrator query failed (is_final={is_final}): {e}")
        await state.on_step("orchestrator", "error", {"error": str(e), "final": is_final})
        return {
            "incident_filed": False, "final_text": f"orchestrator failed: {e}",
            "incident": None, "analysis": None, "decision": None,
        }

    # incident_filed reflects whether submit_incident actually succeeded — NOT
    # whether analyze_failures was called. Those are different things: a call
    # can trigger analysis, conclude everything was business_logic, and file
    # nothing. Conflating the two previously mislabeled the UI ("ESCALATED")
    # on exactly that case.
    incident_filed = state.incident_record is not None
    log_jsonl("orchestrator", {
        "is_final": is_final, "input": summary,
        "output": {"final_text": final_text, "incident_filed": incident_filed},
    })
    await state.on_step("orchestrator", "done", {"final_text": final_text, "incident_filed": incident_filed, "final": is_final})

    # analysis/decision are surfaced here (not just via on_step) so the host
    # (monitor.py) can compose an end-of-test summary from this one return
    # value, without re-deriving it from the individual step broadcasts.
    return {
        "incident_filed": incident_filed,
        "final_text": final_text,
        "incident": state.incident_record,
        "analysis": state.analysis_result,
        "decision": state.decision_result,
    }
