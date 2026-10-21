"""Run the whole demo locally, with presenter controls for the worker.

    uv run python scripts/dev.py            # Temporal, Redis, web on :8000, a v1 worker
    uv run python scripts/dev.py --no-temporal   # use a Temporal you started yourself

It supervises the processes and listens on 127.0.0.1:8765 for the console's controls:

    POST /kill          SIGKILL every worker (the "crash" beat)
    POST /start         start the worker(s) again
    POST /rollout-v2    start a v2 worker and make v2 the current Worker Deployment Version
    POST /rollback-v1   make v1 current again

Workers run as Worker Deployment Versions (build IDs v1 and v2) with Pinned Workflows, so a run
finishes on the version it started on. Logs go to .logs/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".logs"
CONTROL_PORT = 8765
DEPLOYMENT = "agentic-sdlc"
TEMPORAL = ["temporal", "--disable-config-file", "--address", "localhost:7233"]

parser = argparse.ArgumentParser()
parser.add_argument("--no-temporal", action="store_true", help="don't start a Temporal dev server")
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()

LOGS.mkdir(exist_ok=True)
processes: dict[str, subprocess.Popen] = {}
workers: dict[str, subprocess.Popen] = {}  # version -> process
wanted_versions: list[str] = ["v1"]
lock = threading.Lock()


def spawn(name: str, argv: list[str], env: dict | None = None) -> subprocess.Popen:
    log = open(LOGS / f"{name}.log", "a")
    log.write(f"\n--- {time.strftime('%H:%M:%S')} starting {name}\n")
    log.flush()
    return subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, **(env or {})}, start_new_session=True)


def start_worker(version: str) -> None:
    proc = workers.get(version)
    if proc and proc.poll() is None:
        return
    workers[version] = spawn(f"worker-{version}", [sys.executable, "-m", "agentic_sdlc.worker"], {
        "TEMPORAL_WORKER_BUILD_ID": version, "TEMPORAL_DEPLOYMENT_NAME": DEPLOYMENT, "PIPELINE_VERSION": version,
    })


def set_current(version: str) -> str:
    # The version must have polled before it can become current; wait for the worker to start.
    for _ in range(40):
        result = subprocess.run([*TEMPORAL, "worker", "deployment", "set-current-version", "--deployment-name",
                                 DEPLOYMENT, "--build-id", version, "--yes"], capture_output=True, text=True)
        if result.returncode == 0:
            return f"{version} is current"
        time.sleep(0.5)
    return f"couldn't make {version} current: {result.stderr.strip()[:300]}"


def kill_workers() -> int:
    killed = 0
    for proc in workers.values():
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            killed += 1
    return killed


class Control(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        with lock:
            if self.path == "/kill":
                body = {"killed": kill_workers()}
            elif self.path == "/start":
                for version in wanted_versions:
                    start_worker(version)
                body = {"started": wanted_versions}
            elif self.path == "/rollout-v2":
                if "v2" not in wanted_versions:
                    wanted_versions.append("v2")
                start_worker("v2")
                body = {"result": set_current("v2"), "note": "v1 keeps running so pinned runs can finish on it"}
            elif self.path == "/rollback-v1":
                start_worker("v1")
                body = {"result": set_current("v1")}
            else:
                self.send_response(404)
                self.end_headers()
                return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


def wait_for(cmd: list[str], what: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return
        time.sleep(0.5)
    sys.exit(f"{what} didn't come up; see .logs/")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    if not args.no_temporal and subprocess.run([*TEMPORAL, "operator", "cluster", "health"], capture_output=True).returncode != 0:
        processes["temporal"] = spawn("temporal", ["temporal", "server", "start-dev", "--headless",
                                                   "--db-filename", str(LOGS / "temporal.db"), "--log-level", "error"])
        wait_for([*TEMPORAL, "operator", "cluster", "health"], "Temporal")
    if subprocess.run(["redis-cli", "ping"], capture_output=True).returncode != 0:
        if not shutil.which("redis-server"):
            sys.exit("redis-server isn't installed (brew install redis)")
        processes["redis"] = spawn("redis", ["redis-server", "--port", "6379", "--save", "", "--appendonly", "no"])
        wait_for(["redis-cli", "ping"], "Redis")

    start_worker("v1")
    print(set_current("v1"))
    processes["web"] = spawn("web", [sys.executable, "-m", "uvicorn", "agentic_sdlc.web.app:app", "--port", str(args.port)],
                             {"SUPERVISOR_URL": f"http://127.0.0.1:{CONTROL_PORT}"})
    server = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Control)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Console:     http://localhost:{args.port}")
    print("Temporal UI: http://localhost:8233")
    print("Logs:        .logs/   (Ctrl-C stops everything)")
    try:
        while True:
            time.sleep(1)
            for name, proc in list(processes.items()):
                if proc.poll() is not None:
                    print(f"{name} exited ({proc.returncode}); restarting. See .logs/{name}.log")
                    processes[name] = spawn(name, proc.args)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        pass
    finally:
        kill_workers()
        for proc in processes.values():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
