"""
Agent 1 — Monitor.

Watches the live test as fed to it by the host process (monitor.py — this
module makes no network calls of its own). Two responsibilities, combined:

  1. check_gate() / should_invoke(): a cheap, local, rule-based gate deciding
     WHEN a snapshot is worth spending an LLM call on (an endpoint's failure
     rate, a failure-count increase, an rps drop or p95 spike vs. a rolling
     baseline) — plus a heartbeat fallback so the feed is never silent even
     when nothing looks wrong.
  2. classify_snapshot(): the actual LLM call — a single-turn, narrow Google
     ADK query classifying the moment as OK or CONCERNING.

No tools here — Agent 1 is a plain single-turn classifier. Tool-calling is
limited to the downstream Orchestrator — see agents_orchestrator.py.
"""

import json
import logging
import time
from typing import Optional

from adk_runtime import run_agent
from agents_common import log_jsonl, strip_json_fences

logger = logging.getLogger("agents.monitor_agent")

MONITOR_MODEL = "gemini-3.6-flash"

# How often the LLM call fires: at least every HEARTBEAT_SECONDS (so the feed
# isn't silent), or sooner — down to MIN_GATE_INTERVAL — when the gate below trips.
HEARTBEAT_SECONDS = 30
MIN_GATE_INTERVAL = 8

# Rule-based gate thresholds (decide *when* to spend an LLM call, not the verdict)
RPS_DROP_FRACTION = 0.4          # rps falls more than 40% below baseline
P95_SPIKE_FRACTION = 0.6         # p95 rises more than 60% above baseline
ENDPOINT_FAILURE_RATE_PCT = 5.0  # any single endpoint's failure_rate (%) exceeds this
BASELINE_EMA_ALPHA = 0.3
MIN_SNAPSHOTS_BEFORE_BASELINE = 3

SYSTEM_PROMPT = """You are a narrow, read-only monitoring classifier for a Locust load test.
You will be given one JSON snapshot of live metrics plus a recent baseline. Classify the
CURRENT moment as "OK" or "CONCERNING" using only these rules, in order:

1. CONCERNING if any entry in endpoint_failure_rates exceeds 5%.
2. CONCERNING if total_failures increased since the last snapshot (delta_failures > 0)
   and that increase is not trivially explained by a very small total_requests count.
3. CONCERNING if rps has dropped more than ~40% below baseline_rps AND elapsed is past
   the initial ramp-up (elapsed > 10s) — a drop during the first few seconds while users
   are still spawning is normal, not concerning.
4. CONCERNING if p95_response_time has grown more than ~60% above baseline_p95.
5. Otherwise OK.

Weigh these mechanically — do not speculate about causes you cannot see in the data.
Respond with ONLY a single compact JSON object, no other text, no markdown fences:
{"status": "OK" or "CONCERNING", "reason": "<one short sentence, cite the specific number>"}
"""


class MonitorAgentState:
    """Agent 1's own tracking state — baseline + throttle timing. Separate
    from the host's dashboard/UI state, which lives in monitor.py."""

    def __init__(self):
        self.baseline_rps: Optional[float] = None
        self.baseline_avg_rt: Optional[float] = None
        self.baseline_p95: Optional[float] = None
        self.snapshot_count = 0
        self.last_total_failures: Optional[int] = None
        self.last_call_ts: float = 0.0
        # Per-test tally of classify_snapshot verdicts, for the end-of-test
        # summary (monitor.py) — how many checks landed OK vs CONCERNING vs
        # ERROR during this run. Reset alongside the baseline on every new test.
        self.ok_count = 0
        self.concerning_count = 0
        self.error_count = 0

    def reset_baseline(self):
        self.baseline_rps = None
        self.baseline_avg_rt = None
        self.baseline_p95 = None
        self.snapshot_count = 0
        self.last_total_failures = None
        self.ok_count = 0
        self.concerning_count = 0
        self.error_count = 0

    def update_baseline(self, rps: float, avg_rt: float, p95: float):
        if self.baseline_rps is None:
            self.baseline_rps, self.baseline_avg_rt, self.baseline_p95 = rps, avg_rt, p95
        else:
            a = BASELINE_EMA_ALPHA
            self.baseline_rps = a * rps + (1 - a) * self.baseline_rps
            self.baseline_avg_rt = a * avg_rt + (1 - a) * self.baseline_avg_rt
            self.baseline_p95 = a * p95 + (1 - a) * self.baseline_p95
        self.snapshot_count += 1


def check_gate(state: MonitorAgentState, metrics: dict) -> tuple[bool, str]:
    """Pure check — returns (gate_tripped, trigger_reason), no side effects."""
    stats = metrics.get("stats") or []
    for s in stats:
        if s.get("failure_rate", 0) > ENDPOINT_FAILURE_RATE_PCT:
            return True, f"endpoint '{s.get('name')}' failure_rate={s.get('failure_rate'):.1f}%"

    total_failures = metrics.get("total_failures", 0)
    if state.last_total_failures is not None and total_failures > state.last_total_failures:
        return True, f"total_failures increased {state.last_total_failures} -> {total_failures}"

    if state.snapshot_count >= MIN_SNAPSHOTS_BEFORE_BASELINE and state.baseline_rps:
        rps = metrics.get("rps", 0)
        elapsed = metrics.get("elapsed", 0)
        if elapsed > 10 and state.baseline_rps > 0:
            drop = (state.baseline_rps - rps) / state.baseline_rps
            if drop > RPS_DROP_FRACTION:
                return True, f"rps dropped {drop*100:.0f}% below baseline ({rps} vs {state.baseline_rps:.1f})"

        p95 = metrics.get("p95_response_time", 0)
        if state.baseline_p95 and state.baseline_p95 > 0:
            spike = (p95 - state.baseline_p95) / state.baseline_p95
            if spike > P95_SPIKE_FRACTION:
                return True, f"p95 spiked {spike*100:.0f}% above baseline ({p95} vs {state.baseline_p95:.1f})"

    return False, ""


def should_invoke(state: MonitorAgentState, metrics: dict) -> tuple[bool, str]:
    """Combines the rule-based gate with the heartbeat interval. Returns
    (should_call, trigger_reason) — trigger_reason is "heartbeat" whenever
    the gate didn't trip but the heartbeat interval elapsed anyway."""
    gate_tripped, trigger_reason = check_gate(state, metrics)
    elapsed_since_call = time.time() - state.last_call_ts
    should_call = (gate_tripped and elapsed_since_call >= MIN_GATE_INTERVAL) or (
        elapsed_since_call >= HEARTBEAT_SECONDS
    )
    return should_call, (trigger_reason if gate_tripped else "heartbeat")


async def classify_snapshot(state: MonitorAgentState, metrics: dict) -> dict:
    """Agent 1's actual LLM call."""
    endpoint_failure_rates = {
        s.get("name", "?"): round(s.get("failure_rate", 0), 2)
        for s in (metrics.get("stats") or [])
    }
    payload = {
        "rps": metrics.get("rps"),
        "avg_response_time": metrics.get("avg_response_time"),
        "p95_response_time": metrics.get("p95_response_time"),
        "total_failures": metrics.get("total_failures"),
        "delta_failures": (
            metrics.get("total_failures", 0) - state.last_total_failures
            if state.last_total_failures is not None else 0
        ),
        "elapsed": metrics.get("elapsed"),
        "user_count": metrics.get("user_count"),
        "endpoint_failure_rates": endpoint_failure_rates,
        "baseline_rps": round(state.baseline_rps, 2) if state.baseline_rps else None,
        "baseline_avg_rt": round(state.baseline_avg_rt, 2) if state.baseline_avg_rt else None,
        "baseline_p95": round(state.baseline_p95, 2) if state.baseline_p95 else None,
    }

    try:
        result_text = await run_agent(
            name="monitor_classifier", model=MONITOR_MODEL,
            instruction=SYSTEM_PROMPT, prompt=json.dumps(payload),
        )
    except Exception as e:
        logger.error(f"Google ADK monitor call failed: {e}")
        verdict = {"status": "ERROR", "reason": f"agent call failed: {e}"}
        log_jsonl("monitor", {"input": payload, "output": verdict})
        return verdict

    try:
        cleaned = strip_json_fences(result_text)
        verdict = json.loads(cleaned)
        if verdict.get("status") not in ("OK", "CONCERNING"):
            raise ValueError(f"unexpected status: {verdict.get('status')!r}")
    except Exception:
        verdict = {"status": "ERROR", "reason": f"unparseable model output: {result_text[:200]!r}"}

    log_jsonl("monitor", {"input": payload, "output": verdict})
    return verdict
