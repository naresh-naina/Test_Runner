"""
Runtime host for the LocustForge agent pipeline.

This file is NOT itself an agent — it's the process that hosts Agent 1 (the
Monitor, declared in agents_monitor.py) and wires it to everything else:
  - a WebSocket client against the existing /ws/metrics endpoint,
  - the local dashboard (FastAPI, serving agent_ui.html and a WebSocket the
    browser connects to for live updates),
  - handing off to the Orchestrator (agents_orchestrator.py) once a test ends
    with failures, which in turn calls the Analysis (agents_analysis.py) and
    Incident-submission (agents_incident.py) subagents as tool calls.

See agents_monitor.py / agents_orchestrator.py / agents_analysis.py /
agents_incident.py for what each agent actually does, and their docstrings
for where MCP (in-process tool-calling) is declared and used.

Run:
    python agents/monitor.py
Then open http://127.0.0.1:6010 while a test is running via the main UI.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import agents_monitor
import agents_orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("agents.host")

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
load_dotenv(REPO_ROOT / ".env")

# ── Config ───────────────────────────────────────────────────────────────────

MAIN_APP_WS_URL = os.getenv("MONITOR_MAIN_WS_URL", "ws://127.0.0.1:6002/ws/metrics")
_API_KEY = os.getenv("API_KEY", "").strip()

UI_HOST = os.getenv("MONITOR_UI_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("MONITOR_UI_PORT", "6010"))


# ── State ────────────────────────────────────────────────────────────────────

class HostState:
    """Runtime/dashboard state. Agent 1's own tracking state (baseline,
    throttle timing) lives separately in agents_monitor.MonitorAgentState —
    see self.monitor_agent below; this class only holds infra/UI concerns."""

    def __init__(self):
        self.monitor_agent = agents_monitor.MonitorAgentState()
        self.orchestrator_agent = agents_orchestrator.OrchestratorHostState()
        self.last_status: str = "idle"
        self.ui_clients: set[WebSocket] = set()
        # asyncio.create_task() only holds a *weak* reference to the task it
        # returns — if nothing else references it, the event loop can garbage
        # collect (silently cancel) it mid-run. Keep a strong reference here
        # for every background orchestrator run until it finishes.
        self.background_tasks: set[asyncio.Task] = set()


STATE = HostState()


# ── UI broadcast ─────────────────────────────────────────────────────────────

async def broadcast(payload: dict) -> None:
    dead = []
    for ws in STATE.ui_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        STATE.ui_clients.discard(ws)


async def broadcast_agent_step(agent: str, status: str, detail: dict) -> None:
    """agent: 'monitor' | 'orchestrator' | 'analysis' | 'incident'."""
    await broadcast({"type": "agent_step", "agent": agent, "status": status, "detail": detail, "ts": time.time()})


# ── Orchestrator hand-off (Agent 1 -> central decisioning LLM) ─────────────
#
# Two distinct triggers call into this:
#   - is_final=True:  always fires once, when a test ends (pass or fail) —
#     see the "test_ended" branch in handle_snapshot below.
#   - is_final=False: fires mid-run, throttled, whenever Monitor flags a
#     snapshot CONCERNING — see the classify_snapshot branch below. This mode
#     can only investigate (see agents_orchestrator.py); it can never file an
#     incident.

async def run_orchestrator_pipeline(metrics: dict, is_final: bool) -> None:
    if not is_final:
        STATE.orchestrator_agent.midrun_in_flight = True
    try:
        result = await agents_orchestrator.run_orchestrator_for_test(metrics, broadcast_agent_step, is_final=is_final)
        logger.info(
            f"Orchestrator ({'final' if is_final else 'mid-run'}) finished: incident_filed={result['incident_filed']} "
            f"incident={(result.get('incident') or {}).get('incident_number')}"
        )
        if is_final:
            await broadcast_test_summary(metrics, result)
    except Exception as e:
        logger.error(f"Orchestrator pipeline failed ({'final' if is_final else 'mid-run'}): {e}")
        await broadcast_agent_step("orchestrator", "error", {"error": str(e), "final": is_final})
    finally:
        if not is_final:
            STATE.orchestrator_agent.midrun_in_flight = False


# ── End-of-test summary ─────────────────────────────────────────────────────
#
# Code-composed, not a new LLM call: everything below was already produced by
# an agent somewhere in the pipeline (Monitor's verdict tally, Analysis's
# findings, the Orchestrator/Decisioning outcome, the incident record) — this
# just gathers it into one place for the dashboard's "Test Summary" card.

async def broadcast_test_summary(metrics: dict, orchestrator_result: dict) -> None:
    ma = STATE.monitor_agent
    summary = {
        "total_requests": metrics.get("total_requests", 0),
        "total_failures": metrics.get("total_failures", 0),
        "elapsed": metrics.get("elapsed", 0),
        "user_count": metrics.get("user_count", 0),
        "monitor_checks": {"ok": ma.ok_count, "concerning": ma.concerning_count, "error": ma.error_count},
        "findings": orchestrator_result.get("analysis") or [],
        "decision": orchestrator_result.get("decision"),
        "incident_filed": orchestrator_result.get("incident_filed", False),
        "incident": orchestrator_result.get("incident"),
        "final_text": orchestrator_result.get("final_text", ""),
    }
    await broadcast({"type": "test_summary", "summary": summary, "ts": time.time()})


# ── Main monitor loop (WS client against the app being observed) ───────────

async def monitor_loop() -> None:
    while True:
        try:
            extra_headers = {"x-api-key": _API_KEY} if _API_KEY else {}
            async with websockets.connect(MAIN_APP_WS_URL, additional_headers=extra_headers) as ws:
                logger.info(f"Connected to {MAIN_APP_WS_URL}")
                await broadcast({"type": "connection", "connected": True})
                async for raw in ws:
                    metrics = json.loads(raw)
                    await handle_snapshot(metrics)
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            logger.warning(f"Lost connection to main app ({e}); retrying in 3s")
            await broadcast({"type": "connection", "connected": False})
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Unexpected error in monitor loop: {e}")
            await asyncio.sleep(3)


async def handle_snapshot(metrics: dict) -> None:
    status = metrics.get("status", "idle")

    # New test started (or runner reset) since the last snapshot: baseline no
    # longer applies, and any verdict still shown in the UI is from a
    # previous run — tell the dashboard to clear it. Conversely, tell the
    # dashboard when a run ends so it can freeze and label the last verdict
    # instead of leaving it looking "live", and hand off to the orchestrator
    # if the run had any failures.
    if status == "running" and STATE.last_status != "running":
        STATE.monitor_agent.reset_baseline()
        await broadcast({"type": "test_started", "ts": time.time()})
        # Agent 1 is actively watching for the whole duration of the run —
        # reflect that in the dashboard's chip immediately, not just at the end.
        await broadcast_agent_step("monitor", "active", {"note": "Watching live test metrics..."})
    elif status != "running" and STATE.last_status == "running":
        await broadcast({"type": "test_ended", "ts": time.time()})
        total_failures = metrics.get("total_failures", 0)
        await broadcast_agent_step(
            "monitor", "done",
            {"note": f"Test ended — {total_failures} failed request(s) out of {metrics.get('total_requests', 0)}. "
                     f"Orchestrator reviewing the final outcome..."},
        )
        # Final review always runs, pass or fail — the Orchestrator itself
        # decides whether that means "no incident needed" or escalation.
        # Fire-and-forget: don't block the monitor loop on the orchestrator's
        # (potentially multi-turn) run. handle_snapshot must keep returning
        # promptly so new /ws/metrics snapshots (e.g. the next queued test
        # starting) keep being processed. Reference kept in
        # STATE.background_tasks so the task isn't garbage-collected mid-run.
        task = asyncio.create_task(run_orchestrator_pipeline(metrics, is_final=True))
        STATE.background_tasks.add(task)
        task.add_done_callback(STATE.background_tasks.discard)
    STATE.last_status = status

    await broadcast({"type": "snapshot", "metrics": metrics, "ts": time.time()})

    if status != "running":
        return

    # ── Agent 1 (Monitor) invocation — see agents_monitor.py ──────────────
    should_call, trigger_reason = agents_monitor.should_invoke(STATE.monitor_agent, metrics)
    if should_call:
        STATE.monitor_agent.last_call_ts = time.time()
        verdict = await agents_monitor.classify_snapshot(STATE.monitor_agent, metrics)
        verdict_status = verdict.get("status")
        if verdict_status == "OK":
            STATE.monitor_agent.ok_count += 1
        elif verdict_status == "CONCERNING":
            STATE.monitor_agent.concerning_count += 1
        else:
            STATE.monitor_agent.error_count += 1
        ts_label = time.strftime("%H:%M:%S")
        reason_note = " [heartbeat]" if trigger_reason == "heartbeat" else f" [triggered by: {trigger_reason}]"
        print(f"[{ts_label}] {verdict.get('status')}: {verdict.get('reason')}{reason_note}")
        await broadcast({
            "type": "verdict",
            "verdict": verdict,
            "trigger": trigger_reason,
            "ts": time.time(),
        })

        # ── Mid-run Orchestrator hand-off — investigate-only, throttled ──
        if verdict.get("status") == "CONCERNING" and agents_orchestrator.should_invoke_midrun(STATE.orchestrator_agent):
            STATE.orchestrator_agent.last_midrun_call_ts = time.time()
            task = asyncio.create_task(run_orchestrator_pipeline(metrics, is_final=False))
            STATE.background_tasks.add(task)
            task.add_done_callback(STATE.background_tasks.discard)

    STATE.monitor_agent.update_baseline(
        metrics.get("rps", 0),
        metrics.get("avg_response_time", 0),
        metrics.get("p95_response_time", 0),
    )
    STATE.monitor_agent.last_total_failures = metrics.get("total_failures", 0)


# ── Tiny local dashboard (FastAPI) ──────────────────────────────────────────

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_task
    _monitor_task = asyncio.create_task(monitor_loop(), name="monitor_loop")
    logger.info(f"Agent monitor watching {MAIN_APP_WS_URL}")
    yield
    if _monitor_task:
        _monitor_task.cancel()


app = FastAPI(title="LocustForge Agent Monitor", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def root():
    path = BASE_DIR / "templates" / "agent_ui.html"
    return path.read_text(encoding="utf-8")


@app.websocket("/agent-ws")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    STATE.ui_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep the connection open; UI doesn't send anything
    except WebSocketDisconnect:
        pass
    finally:
        STATE.ui_clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=UI_HOST, port=UI_PORT)
