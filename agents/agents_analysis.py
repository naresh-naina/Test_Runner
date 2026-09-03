"""
Agent 2 — Analysis (subagent).

Given the real per-endpoint stats and raw error entries collected during a
finished test run, identifies which APIs failed and the most likely root
cause — grounded in the actual error content (including any response body
captured on failure; see utils/script_generator.py) rather than fabricated.

### Google ADK tool ###
This agent is invoked by the Orchestrator through the native ADK function tool
returned from build_analyze_failures_tool(). The function closes over the
current run state; no MCP server or subprocess is required.
"""

import json
import logging

from adk_runtime import run_agent
from agents_common import log_jsonl, strip_json_fences

logger = logging.getLogger("agents.analysis")

ANALYSIS_MODEL = "gemini-3.6-flash"

ANALYSIS_SYSTEM_PROMPT = """You are a narrow, read-only analysis agent for a Locust load test.
You are given the test's per-endpoint stats and raw error entries collected during the run.
Each raw error's "Error" field may be a plain HTTP status line (e.g. "Got status 500"), or
"Got status <code>: <body>" where <body> is the *actual response text* the failing API returned.

When <body> is JSON, the downstream services in this environment commonly use this shape — learn
its field names, they are the most reliable signal you have:
  {"status": <int>, "code": "<MACHINE_READABLE_ENUM>", "error": "<human-readable message>",
   "exception": "<fully-qualified exception class + message, e.g. a Java stack-trace-style string>"
   -- OR --
   "details": {<structured extra context specific to this error, e.g. rate-limit numbers>},
   "timestamp": "<ISO-8601>"}
"error" here is always the plain human-readable message — do not confuse it with this analysis
agent's own output field of the same name (see the output "error" object below, which is your
answer, not theirs). "code" is a fixed, machine-readable enum (e.g. "ACCESS_DENIED",
"RATE_LIMIT_EXCEEDED") and is usually your single best root-cause signal — prefer citing it
explicitly over paraphrasing the free-text "error" message. Not every failing endpoint will use
this exact shape (some APIs, or connection-level failures, won't have a body at all) — use it when
present, don't assume it's the only shape you'll see.

IMPORTANT: "failure" here just means the API returned an HTTP status >= 400 — that is a raw
mechanical count, not a judgment. Some of these are the API correctly reporting an expected
business-logic outcome (the system working exactly as designed), not a fault. You must classify
each failing endpoint into one of:

- "business_logic" — a clear, well-formed application-level message describing a normal outcome
  the system is designed to report. For example: "No records available", "No balance remaining",
  "No accounts present", "Item out of stock", "User not found", "Insufficient funds". These
  typically pair with a plain, human-readable message and no exception class name, no stack trace,
  no nested "cause" chain. Treat that pattern as the signal, not the specific wording — real APIs
  phrase this differently everywhere; the examples above are illustrative, not an exhaustive list
  to match against. This bucket also covers a "details" object (no "exception") that documents the
  service correctly enforcing a stated policy rather than something breaking — e.g. a 429 with
  "code": "RATE_LIMIT_EXCEEDED" and a "details" object giving the limit/retry-after: the service is
  behaving exactly as configured, not malfunctioning, so this is business_logic, not a fault. An
  "exception" field alone does NOT rule this out — some frameworks report an expected, anticipated
  condition as a thrown exception rather than a clean message. Judge the exception by what it
  actually represents, not merely its presence: "MaxUploadSizeExceededException" (a configured size
  limit correctly enforced), "ExpiredJwtException" (normal auth-lifecycle expiry, the client just
  needs to refresh), or "OptimisticLockException" (expected concurrent-write contention, not
  corruption) all describe the system correctly enforcing a rule or handling a routine condition —
  business_logic, not a fault — even though each is, technically, an exception.
- "system_fault" — the response indicates the service itself broke or misbehaved: 5xx status
  codes, a stack trace or nested "cause" chain, a connection-level failure (timeout, connection
  reset) with no response body at all, or an exception that represents *unexpected internal
  breakage* rather than an anticipated condition — e.g. "NullPointerException",
  "SQLTransientConnectionException", a malformed/unparseable response from a downstream dependency,
  or (explicitly, regardless of how it's caused) "AccessDeniedException" / any permission-denied
  exception on a 401/403: even though that one often turns out to be the load test's own
  credentials rather than a production bug, always treat it as system_fault and let a human sort out
  which it was — do not try to guess that distinction yourself. This is the one exception-type call
  you should NOT make using the "expected condition" judgment above; always flag it.
- "unclear" — you cannot confidently tell which of the above it is from the given data. Default to
  this rather than guessing "business_logic" when uncertain — a missed real fault is worse than an
  unnecessary flag.

For each endpoint that has any failures, identify:
- the API path and HTTP method,
- "category": one of "business_logic" | "system_fault" | "unclear", per the rules above,
- the most likely root cause. When a JSON body is present, lead with its "code" value if present
  (it's the most precise signal you have), then ground the rest in the body's real content — do
  not invent an exception class name or code that isn't in the data. If only a bare status code or
  a connection-level error (e.g. ConnectionResetError, timeout) is available with no body, describe
  the cause generically (e.g. "downstream returned no response body; connection was reset,
  consistent with the backend being overloaded or restarting") rather than fabricating specifics
  you cannot see.
- a structured "error" object to carry forward: if the raw error contains a parseable JSON body,
  pass its fields through as closely as possible — "status", "code", "message" (the body's own
  "error"/human-message field, renamed here to avoid colliding with this object's own name),
  "exception" and/or "details", whichever were present. Include every one of these that was present
  in the source body, regardless of which category you assigned — do NOT drop "exception" (or
  "details") just because you judged the endpoint business_logic; a human reading this later needs
  to see the same evidence you based that judgment on. If no JSON body is present, construct a
  minimal object with at least "status" (if known) and "message" describing what actually happened.

Respond with ONLY a compact JSON array, no other text, no markdown fences, one entry per failing
endpoint (skip endpoints with zero failures):
[{"api": "<path>", "method": "<HTTP method>", "category": "business_logic"|"system_fault"|"unclear",
  "likely_cause": "<one or two sentences, lead with the code if one was present>",
  "error": {<the structured error object described above>}}]
"""


async def run_analysis(final_metrics: dict) -> list[dict]:
    """Agent 2's actual LLM call — a single-turn query with no tools of its
    own (tools=[]); it only reasons over the data it's handed."""
    stats = final_metrics.get("stats") or []
    errors = final_metrics.get("errors") or []
    failing_stats = [s for s in stats if s.get("num_failures", 0) > 0]
    payload = {"failing_endpoints": failing_stats, "raw_errors": errors}

    result_text = await run_agent(
        name="failure_analysis", model=ANALYSIS_MODEL,
        instruction=ANALYSIS_SYSTEM_PROMPT, prompt=json.dumps(payload),
    )

    cleaned = strip_json_fences(result_text)
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array from analysis, got: {result_text[:200]!r}")
    log_jsonl("analysis", {"input": payload, "output": data})
    return data


def build_analyze_failures_tool(state):
    """Return the native ADK tool function for this orchestrator run."""

    async def analyze_failures() -> dict:
        """Inspect errors and endpoint stats. Call before deciding or filing an incident."""
        await state.on_step("analysis", "active", {"note": "Inspecting failed endpoints..."})
        try:
            result = await run_analysis(state.final_metrics)
        except Exception as e:
            logger.error(f"analyze_failures failed: {e}")
            await state.on_step("analysis", "error", {"error": str(e)})
            return {"error": f"Analysis failed: {e}"}
        state.analysis_result = result
        await state.on_step("analysis", "done", {"result": result})
        return {"findings": result}

    return analyze_failures
