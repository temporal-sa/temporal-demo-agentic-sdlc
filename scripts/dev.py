"""Run the whole demo locally, with controls for the worker process.

    uv run python scripts/dev.py            # Temporal, Redis, web on :8000, v1 and v2 workers, GitHub webhooks
    uv run python scripts/dev.py --no-temporal   # use a Temporal you started yourself

GitHub reaches a laptop through `gh webhook forward` (the cli/gh-webhook extension, installed if
missing), which relays the storefront repo's events to localhost:8000/webhooks/github while this
runs. GitHub allows one forwarder per repo at a time; if someone else is forwarding, this stops
and says so. Your gh login needs the admin:repo_hook scope (`gh auth refresh -s admin:repo_hook`).

This laptop is its own copy of the demo: task queue `local-<your GitHub login>`, and it acts only
on issues and PRs labeled with one of its session slots (`agentic-sdlc:local-<login>--s01`, ...), which
"Start a scenario" adds. The hosted
demo ignores that label, so the two never pick up each other's work.

It supervises the processes and listens on 127.0.0.1:8765 for the console's controls:

    POST /kill          SIGKILL every worker process (the console's "Crash my worker" stops just the session's Workers)
    POST /start         start the workers again

It runs one worker per pipeline version (build IDs v1 and v2), as the catalog does. The console
makes a version current through Temporal (agentic_sdlc/versions.py); runs are Pinned, so a run
finishes on the version it started on. Logs go to .logs/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
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
TEMPORAL = ["temporal", "--disable-config-file", "--address", "localhost:7233"]
WEBHOOK_EVENTS = "issues,issue_comment,pull_request,pull_request_review,pull_request_review_comment"
SEARCH_ATTRIBUTE = ["--name", "AgenticSdlcGitHubRefs", "--type", "KeywordList"]

parser = argparse.ArgumentParser()
parser.add_argument("--no-temporal", action="store_true", help="don't start a Temporal dev server")
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()

LOGS.mkdir(exist_ok=True)
processes: dict[str, subprocess.Popen] = {}
envs: dict[str, dict] = {}  # each process's extra environment, reused when it restarts
workers: dict[str, subprocess.Popen] = {}  # version -> process
wanted_versions: list[str] = ["v1", "v2"]
lock = threading.Lock()
copy_env: dict[str, str] = {}  # this laptop's copy of the demo, for the web server and the workers


def gh_login() -> str:
    found = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    if found.returncode != 0:
        sys.exit("The GitHub CLI isn't signed in. Run `gh auth login`; webhooks reach this laptop through it.")
    return re.sub(r"[^a-z0-9-]", "-", found.stdout.strip().lower())


def this_copy() -> dict[str, str]:
    """Task queue, owner label and a webhook secret for this laptop's copy of the demo."""
    name = f"local-{gh_login()}"
    return {
        "TEMPORAL_TASK_QUEUE": name,
        "OWNER_LABEL": f"agentic-sdlc:{name}",
        # The forwarder signs what it relays with this, and the web server checks it.
        "GITHUB_WEBHOOK_SECRET": os.environ.get("GITHUB_WEBHOOK_SECRET") or secrets.token_hex(20),
        "PUBLIC_URL": f"http://localhost:{args.port}",
    }


def repo() -> str:
    for line in (ROOT / ".env.local").read_text().splitlines() if (ROOT / ".env.local").exists() else []:
        if line.startswith("GITHUB_SANDBOX_REPO="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("GITHUB_SANDBOX_REPO", "temporal-sa/agentic-sdlc-storefront")


def forward_webhooks() -> None:
    """Relay the storefront repo's GitHub events to the local web server."""
    extensions = subprocess.run(["gh", "extension", "list"], capture_output=True, text=True).stdout
    if "gh-webhook" not in extensions:
        print("Installing the gh webhook extension (cli/gh-webhook)…")
        if subprocess.run(["gh", "extension", "install", "cli/gh-webhook"]).returncode != 0:
            sys.exit("Couldn't install cli/gh-webhook; GitHub's webhooks can't reach this laptop without it.")
    processes["webhooks"] = spawn("webhooks", [
        "gh", "webhook", "forward", f"--repo={repo()}", f"--events={WEBHOOK_EVENTS}",
        f"--url=http://localhost:{args.port}/webhooks/github", f"--secret={copy_env['GITHUB_WEBHOOK_SECRET']}",
    ])
    time.sleep(4)
    if processes["webhooks"].poll() is not None:
        tail = (LOGS / "webhooks.log").read_text().strip().splitlines()[-5:]
        sys.exit("GitHub webhook forwarding didn't start:\n  " + "\n  ".join(tail) + "\n"
                 "If someone else is forwarding this repo, only one forwarder can run at a time. If it's a "
                 "permissions error, run `gh auth refresh -s admin:repo_hook`.")


def spawn(name: str, argv: list[str], env: dict | None = None) -> subprocess.Popen:
    envs[name] = env or {}
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
        **copy_env, "TEMPORAL_WORKER_BUILD_ID": version, "PIPELINE_VERSION": version,
    })


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
        processes["temporal"] = spawn("temporal", ["temporal", "server", "start-dev", "--ui-port", "8233",
                                                   "--db-filename", str(LOGS / "temporal.db"), "--log-level", "error"])
        wait_for([*TEMPORAL, "operator", "cluster", "health"], "Temporal")
    if subprocess.run(["redis-cli", "ping"], capture_output=True).returncode != 0:
        if not shutil.which("redis-server"):
            sys.exit("redis-server isn't installed (brew install redis)")
        processes["redis"] = spawn("redis", ["redis-server", "--port", "6379", "--save", "", "--appendonly", "no"])
        wait_for(["redis-cli", "ping"], "Redis")
    # The webhook finds the runs watching an issue or PR by this search attribute.
    exists = subprocess.run([*TEMPORAL, "operator", "search-attribute", "list"], capture_output=True, text=True).stdout
    if "AgenticSdlcGitHubRefs" not in exists:
        subprocess.run([*TEMPORAL, "operator", "search-attribute", "create", *SEARCH_ATTRIBUTE], check=True, capture_output=True)

    copy_env.update(this_copy())
    for version in wanted_versions:
        start_worker(version)
    processes["web"] = spawn("web", [sys.executable, "-m", "uvicorn", "agentic_sdlc.web.app:app", "--port", str(args.port)],
                             {**copy_env, "SUPERVISOR_URL": f"http://127.0.0.1:{CONTROL_PORT}"})
    forward_webhooks()
    server = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Control)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Console:     http://localhost:{args.port}")
    print("Temporal UI: http://localhost:8233")
    print(f"GitHub:      {repo()} events forwarded here; this copy acts on items labeled {copy_env['OWNER_LABEL']}")
    print("Logs:        .logs/   (Ctrl-C stops everything)")
    try:
        while True:
            time.sleep(1)
            for name, proc in list(processes.items()):
                if proc.poll() is not None:
                    print(f"{name} exited ({proc.returncode}); restarting. See .logs/{name}.log")
                    processes[name] = spawn(name, proc.args, envs.get(name))  # type: ignore[arg-type]
    except KeyboardInterrupt:
        pass
    finally:
        kill_workers()
        for proc in processes.values():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
