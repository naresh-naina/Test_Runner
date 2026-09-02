"""
Decisioning agent — the actual judgment call.

Given Analysis's categorized findings, decides whether this test's failures
warrant filing an incident ("escalate") or not ("pass"). Deliberately
separated from the Orchestrator (agents_orchestrator.py): the Orchestrator's
job is routing — which agent to invoke, in what order — while this agent's
job is the one substantive judgment call in the whole pipeline. Splitting
them keeps each narrow and single-purpose, matching every other agent here
(Monitor classifies, Analysis categorizes, Incident submits — none of them
also decide something else on the side).

Only used in "final" mode. Mid-run interim checks (agents_orchestrator.py,
is_final=False) skip Decisioning entirely — there is nothing for a decision
to gate mid-run, since submit_incident isn't even a registered tool there.

### MCP ###
Wrapped as an in-process MCP tool ("decide_escalation") the same way as
agents_analysis.py's tool — see the docstring on build_analyze_failures_tool()
there for how create_sdk_mcp_server() wires it into the Orchestrator's
session in agents_orchestrator.py.
"""

import json
import logging

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query, tool

from agents_common import log_jsonl, strip_json_fences

logger = logging.getLogger("agents.decisioning")

DECISION_MODEL = "claude-sonnet-5"

DECISION_SYSTEM_PROMPT = """You are the decisioning agent for a load-testing pipeline. You are
given a categorized list of endpoints that failed during a test, produced by a separate analysis
step. Each entry has a "category" of "business_logic" (the API correctly reporting an expected
outcome — the system working as designed, not a fault), "system_fault" (a genuine problem: 5xx,
an exception, a stack trace, a connection-level failure), or "unclear" (not confident either way).

Decide whether this run warrants filing an incident:
- If every entry is "business_logic", decide "pass" — nothing to escalate; the system behaved
  correctly throughout.
- If any entry is "system_fault" or "unclear", decide "escalate" — a genuine problem (or one that
  cannot be ruled out) occurred and should be filed.

Respond with ONLY a compact JSON object, no other text, no markdown fences:
{"decision": "escalate" or "pass", "reasoning": "<one or two sentences citing what drove the decision>"}
"""


async def run_decisioning(analysis_result: list[dict]) -> dict:
    """The actual nested LLM call — a single-turn query with no tools of its
    own (tools=[]); it only judges the categorized findings it's handed."""
    payload = {"findings": analysis_result}

    options = ClaudeAgentOptions(
        model=DECISION_MODEL,
        system_prompt=DECISION_SYSTEM_PROMPT,
        permission_mode="dontAsk",
        tools=[],
        allowed_tools=[],
        max_turns=1,
    )

    result_text = ""
    async for message in query(prompt=json.dumps(payload), options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""

    cleaned = strip_json_fences(result_text)
    data = json.loads(cleaned)
    if data.get("decision") not in ("escalate", "pass"):
        raise ValueError(f"unexpected decision value: {data.get('decision')!r}")
    log_jsonl("decisioning", {"input": payload, "output": data})
    return data


def build_decide_escalation_tool(state):
    """### MCP TOOL DECLARATION ### — see agents_analysis.py's
    build_analyze_failures_tool() for the general mechanism. Requires
    analyze_failures to have already run this session (reads
    state.analysis_result) — refuses otherwise, the same enforcement pattern
    used by submit_incident.
    """

    @tool(
        "decide_escalation",
        "Given the most recent analyze_failures result, decide whether this test's failures "
        "warrant filing an incident ('escalate') or not ('pass'). Call this after "
        "analyze_failures and before submit_incident.",
        {},
    )
    async def decide_escalation_tool(args):
        if state.analysis_result is None:
            return {
                "content": [{
                    "type": "text",
                    "text": "ERROR: analyze_failures has not been called yet this run — call it first.",
                }],
                "is_error": True,
            }
        await state.on_step("decisioning", "active", {"note": "Deciding whether to escalate..."})
        try:
            result = await run_decisioning(state.analysis_result)
        except Exception as e:
            logger.error(f"decide_escalation failed: {e}")
            await state.on_step("decisioning", "error", {"error": str(e)})
            return {"content": [{"type": "text", "text": f"Decisioning failed: {e}"}], "is_error": True}
        state.decision_result = result
        await state.on_step("decisioning", "done", {"result": result})
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return decide_escalation_tool
