"""
Agent 3 — Incident submission (subagent).

Deliberately NOT an LLM step — deterministic HTTP only. Files one incident
per test with the downstream incident API using Agent 2's (Analysis) result,
then verifies the record round-trips via a follow-up GET.

### MCP ###
Wrapped as an in-process MCP tool ("submit_incident") the same way as
agents_analysis.py's tool — see the docstring on build_analyze_failures_tool()
there for how create_sdk_mcp_server() wires it into the Orchestrator's
session in agents_orchestrator.py.
"""

import json
import logging
import time

import httpx
from claude_agent_sdk import tool

from agents_common import log_jsonl

logger = logging.getLogger("agents.incident")

INCIDENT_API_BASE = "http://127.0.0.1:5006"


def build_submit_incident_tool(state):
    """### MCP TOOL DECLARATION ### — see agents_analysis.py's
    build_analyze_failures_tool() for the general mechanism. This tool
    additionally *enforces*, in code rather than only via the Orchestrator's
    system prompt, two invariants:
      1. It refuses to run unless analyze_failures already populated
         state.analysis_result earlier in this same run.
      2. It never files an incident for endpoints Analysis categorized as
         "business_logic" (the API correctly reporting an expected outcome,
         e.g. "No balance remaining" — not a fault). If every analyzed
         endpoint turns out to be business_logic, it refuses outright rather
         than filing a vacuous incident, even if the Orchestrator's judgment
         somehow still tried to call this.
    """

    @tool(
        "submit_incident",
        "File an incident with the downstream incident API using the most recent "
        "analyze_failures result. Requires analyze_failures to have been called first this run. "
        "Endpoints categorized as business_logic are never included.",
        {"test_time": str},
    )
    async def submit_incident_tool(args):
        if state.analysis_result is None:
            return {
                "content": [{
                    "type": "text",
                    "text": "ERROR: analyze_failures has not been called yet this run — call it first.",
                }],
                "is_error": True,
            }

        # Code-level filter: business_logic findings are the system working as
        # designed, not a fault, and must never appear in a filed incident.
        genuine = [a for a in state.analysis_result if a.get("category") != "business_logic"]
        if not genuine:
            return {
                "content": [{
                    "type": "text",
                    "text": "Refusing to file an incident: every analyzed endpoint was categorized "
                            "as an expected business-logic response, not a system fault — there is "
                            "nothing to escalate.",
                }],
                "is_error": True,
            }

        await state.on_step("incident", "active", {"note": "Submitting incident..."})
        payload = {
            "test_time": args.get("test_time") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "apis": [
                {"api": a.get("api"), "method": a.get("method"), "error": a.get("error")}
                for a in genuine
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{INCIDENT_API_BASE}/incidents", json=payload)
                resp.raise_for_status()
                record = resp.json()

                # Round-trip check against GET /incidents/{incident_number} — confirms
                # the record was actually persisted, not just accepted, and exercises
                # the second endpoint the incident API exposes.
                verified = False
                incident_number = record.get("incident_number")
                if incident_number is not None:
                    try:
                        get_resp = await client.get(f"{INCIDENT_API_BASE}/incidents/{incident_number}")
                        verified = get_resp.status_code == 200
                    except Exception as verify_exc:
                        logger.warning(f"Incident verification GET failed: {verify_exc}")
        except Exception as e:
            logger.error(f"submit_incident failed: {e}")
            log_jsonl("incident", {"input": payload, "output": {"error": str(e)}})
            await state.on_step("incident", "error", {"error": str(e), "payload": payload})
            return {"content": [{"type": "text", "text": f"Incident submission failed: {e}"}], "is_error": True}

        state.incident_record = record
        log_jsonl("incident", {"input": payload, "output": record})
        await state.on_step("incident", "done", {"record": record, "verified": verified})
        return {"content": [{"type": "text", "text": json.dumps(record)}]}

    return submit_incident_tool
