"""
Agent 3 — Incident submission (subagent).

Deliberately NOT an LLM step — deterministic HTTP only. Files one incident
per test with the downstream incident API using Agent 2's (Analysis) result,
then verifies the record round-trips via a follow-up GET.

### Google ADK tool ###
Registered as a native ADK function tool ("submit_incident") by the
Orchestrator. The closure keeps it scoped to one test run.
"""

import json
import logging
import time

import httpx
from agents_common import log_jsonl

logger = logging.getLogger("agents.incident")

INCIDENT_API_BASE = "http://127.0.0.1:5006"


def build_submit_incident_tool(state):
    """Return the native ADK incident tool for this run.

    The tool additionally *enforces*, in code rather than only via the Orchestrator's
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

    async def submit_incident(test_time: str = "") -> dict:
        """File an incident from analyzed failures. Call only after an escalate decision."""
        if state.analysis_result is None:
            return {"error": "analyze_failures has not been called yet this run"}

        # Code-level filter: business_logic findings are the system working as
        # designed, not a fault, and must never appear in a filed incident.
        genuine = [a for a in state.analysis_result if a.get("category") != "business_logic"]
        if not genuine:
            return {"error": "all analyzed endpoints were expected business-logic responses"}

        await state.on_step("incident", "active", {"note": "Submitting incident..."})
        payload = {
            "test_time": test_time or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
            return {"error": f"Incident submission failed: {e}"}

        state.incident_record = record
        log_jsonl("incident", {"input": payload, "output": record})
        await state.on_step("incident", "done", {"record": record, "verified": verified})
        return {"incident": record, "verified": verified}

    return submit_incident
