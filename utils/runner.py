import asyncio
import csv
import subprocess
import time
import os
import signal
import tempfile
import shutil
import logging
from typing import Optional

from models import TestConfig, TestMetrics, TestStatus, RequestStat
from utils.script_generator import generate_locust_script

logger = logging.getLogger(__name__)


class LocustRunner:
    """Manages a single Locust test run as a subprocess with CSV stats collection."""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._script_path: Optional[str] = None
        self._stats_dir: Optional[str] = None
        self._config: Optional[TestConfig] = None
        self._status: TestStatus = TestStatus.IDLE
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._stats_file: Optional[str] = None
        self.timeseries: list[dict] = []
        self._ts_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self.history_target: str = "local"
        self._run_saved: bool = False

    @staticmethod
    def _sf(v) -> float:
        try:
            return float(v or 0)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _si(v) -> int:
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0

    @property
    def status(self) -> TestStatus:
        if self._process is None:
            return self._status
        poll = self._process.poll()
        if poll is None:
            return TestStatus.RUNNING
        if self._status == TestStatus.STOPPING:
            return TestStatus.COMPLETED
        return TestStatus.COMPLETED if poll == 0 else TestStatus.FAILED

    def is_running(self) -> bool:
        return self.status == TestStatus.RUNNING

    async def start(self, config: TestConfig) -> None:
        if self.is_running():
            raise RuntimeError("A test is already running. Stop it first.")

        self._config = config
        self._status = TestStatus.RUNNING
        self._start_time = time.time()
        self._run_saved = False
        self.timeseries = []

        script_content = generate_locust_script(config)
        tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", prefix="locust_")
        tmp.write(script_content)
        tmp.flush()
        tmp.close()
        self._script_path = tmp.name

        self._stats_dir = tempfile.mkdtemp(prefix="locust_stats_")
        self._stats_file = os.path.join(self._stats_dir, "stats")

        cmd = [
            "locust", "-f", self._script_path,
            "--headless",
            "--users", str(config.users),
            "--spawn-rate", str(config.spawn_rate),
            "--run-time", f"{config.duration}s",
            "--csv", self._stats_file,
            "--csv-full-history",
            "--host", config.base_url,
            "--logfile", os.path.join(self._stats_dir, "locust.log"),
        ]

        logger.info(f"Starting locust: {' '.join(cmd)}")
        # Redirect to files rather than PIPE: nothing in this process drains
        # stdout/stderr, and locust logs to stderr even with --logfile set
        # (plus a periodic stats table on stdout in headless mode). Piping
        # without reading fills the OS pipe buffer and deadlocks the
        # single-threaded locust process once it blocks on a write().
        stdout_f = open(os.path.join(self._stats_dir, "locust_stdout.log"), "wb")
        stderr_f = open(os.path.join(self._stats_dir, "locust_stderr.log"), "wb")
        try:
            if os.name == "nt":
                self._process = subprocess.Popen(
                    cmd,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    preexec_fn=os.setsid,
                )
        finally:
            stdout_f.close()
            stderr_f.close()

        self._watch_task = asyncio.create_task(self._watch_process())
        self._ts_task = asyncio.create_task(self._collect_timeseries())

    async def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._status = TestStatus.STOPPING
            try:
                if os.name == "nt":
                    self._process.send_signal(signal.CTRL_BREAK_EVENT)
                    await asyncio.sleep(2)
                    if self._process.poll() is None:
                        self._process.terminate()
                else:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                    await asyncio.sleep(2)
                    if self._process.poll() is None:
                        os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.warning(f"Error stopping locust process: {exc}")
        self._status = TestStatus.COMPLETED
        # Capture one final snapshot after process terminates
        try:
            snap = self._parse_aggregate()
            if snap:
                snap["t"] = round(time.time() - self._start_time, 1) if self._start_time else 0
                if not self.timeseries or self.timeseries[-1].get("t") != snap["t"]:
                    self.timeseries.append(snap)
        except Exception:
            pass

    async def _watch_process(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._process.wait)
        if self._status == TestStatus.RUNNING:
            self._status = TestStatus.COMPLETED
        self._end_time = time.time()
        # Capture a final timeseries point when process exits naturally
        try:
            snap = self._parse_aggregate()
            if snap:
                snap["t"] = round(self._end_time - self._start_time, 1) if self._start_time else 0
                if not self.timeseries or self.timeseries[-1].get("t") != snap["t"]:
                    self.timeseries.append(snap)
        except Exception:
            pass
        logger.info(f"Locust process ended with returncode {self._process.returncode}")

    async def _collect_timeseries(self):
        while self.is_running():
            await asyncio.sleep(2)
            if not self.is_running():
                break
            try:
                snap = self._parse_aggregate()
                if snap:
                    snap["t"] = round(time.time() - self._start_time, 1)
                    self.timeseries.append(snap)
            except Exception as e:
                logger.debug(f"Timeseries error: {e}")

    def _parse_aggregate(self) -> Optional[dict]:
        stats_csv = (self._stats_file or "") + "_stats.csv"
        if not os.path.exists(stats_csv):
            return None
        with open(stats_csv, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("Name", "").strip() == "Aggregated":
                    return {
                        "rps":       round(self._sf(row.get("Requests/s", 0)), 2),
                        "avg_rt":    round(self._sf(row.get("Average Response Time", 0)), 1),
                        "p95_rt":    round(self._sf(row.get("95%", 0)), 1),
                        "p99_rt":    round(self._sf(row.get("99%", 0)), 1),
                        "total_req": self._si(row.get("Request Count", 0)),
                        "failures":  self._si(row.get("Failure Count", 0)),
                        "users":     self._config.users if self._config else 0,
                    }
        return None

    def get_metrics(self) -> TestMetrics:
        elapsed = (time.time() - self._start_time) if self._start_time else 0.0
        stats_list: list[RequestStat] = []
        errors_list = []
        total_requests = 0
        total_failures = 0
        total_rps = 0.0
        avg_rt = 0.0
        p95_rt = 0.0

        stats_csv = (self._stats_file or "") + "_stats.csv"
        if os.path.exists(stats_csv):
            try:
                with open(stats_csv, newline="") as f:
                    rows = list(csv.DictReader(f))
                aggregate = None
                endpoint_rows = []
                for row in rows:
                    if row.get("Name", "").strip() == "Aggregated":
                        aggregate = row
                    else:
                        endpoint_rows.append(row)

                for row in endpoint_rows:
                    nr = self._si(row.get("Request Count", 0))
                    nf = self._si(row.get("Failure Count", 0))
                    stats_list.append(RequestStat(
                        name=row.get("Name", ""),
                        method=row.get("Type", ""),
                        num_requests=nr,
                        num_failures=nf,
                        avg_response_time=self._sf(row.get("Average Response Time", 0)),
                        min_response_time=self._sf(row.get("Min Response Time", 0)),
                        max_response_time=self._sf(row.get("Max Response Time", 0)),
                        p50=self._sf(row.get("50%", 0)),
                        p95=self._sf(row.get("95%", 0)),
                        p99=self._sf(row.get("99%", 0)),
                        rps=self._sf(row.get("Requests/s", 0)),
                        failure_rate=(nf / nr * 100) if nr > 0 else 0.0,
                    ))
                if aggregate:
                    total_requests = self._si(aggregate.get("Request Count", 0))
                    total_failures = self._si(aggregate.get("Failure Count", 0))
                    total_rps = self._sf(aggregate.get("Requests/s", 0))
                    avg_rt = self._sf(aggregate.get("Average Response Time", 0))
                    p95_rt = self._sf(aggregate.get("95%", 0))
            except Exception as e:
                logger.warning(f"Could not parse stats CSV: {e}")

        errors_csv = (self._stats_file or "") + "_failures.csv"
        if os.path.exists(errors_csv):
            try:
                with open(errors_csv, newline="") as f:
                    for row in csv.DictReader(f):
                        errors_list.append(dict(row))
            except Exception as e:
                logger.warning(f"Could not parse errors CSV: {e}")

        return TestMetrics(
            status=self.status,
            elapsed=round(elapsed, 1),
            total_requests=total_requests,
            total_failures=total_failures,
            rps=round(total_rps, 2),
            avg_response_time=round(avg_rt, 1),
            p95_response_time=round(p95_rt, 1),
            user_count=self._config.users if self._config else 0,
            stats=stats_list,
            errors=errors_list,
        )

    def get_script(self) -> Optional[str]:
        if self._script_path and os.path.exists(self._script_path):
            with open(self._script_path) as f:
                return f.read()
        return None

    def reset(self):
        if self.is_running():
            raise RuntimeError("Cannot reset while test is running.")
        # Cancel any lingering background tasks
        for task in (self._ts_task, self._watch_task):
            if task and not task.done():
                task.cancel()
        self._ts_task = None
        self._watch_task = None
        # Clean up temp files to avoid disk leaks
        if self._script_path and os.path.exists(self._script_path):
            try:
                os.unlink(self._script_path)
            except OSError:
                pass
        if self._stats_dir and os.path.isdir(self._stats_dir):
            try:
                shutil.rmtree(self._stats_dir)
            except OSError:
                pass
        self._process = None
        self._config = None
        self._status = TestStatus.IDLE
        self._start_time = None
        self._end_time = None
        self._script_path = None
        self._stats_dir = None
        self._stats_file = None
        self._run_saved = False
        self.timeseries = []
