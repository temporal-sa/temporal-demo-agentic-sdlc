"""End-to-end check without the UI: start a scenario, then answer every gate on GitHub with `gh`.

    uv run python scripts/drive_run.py dependency   # opens the bump PR with gh, as the presenter would
    uv run python scripts/drive_run.py review [--ask "Please fix the rounding"]
    uv run python scripts/drive_run.py issue [--issue free-shipping] [--break-canary] [--hold production]

Like a presenter would, it starts the scenario through the console's API, then acts only on
GitHub: `/approve`, `/ship`, `/continue`, or a comment. A dependency or review run ends when the
script closes its PR. Needs scripts/dev.py (or a hosted copy) receiving the repo's webhooks.
"""

import argparse
import subprocess
import time
from urllib.parse import parse_qs, urlparse

import httpx

from agentic_sdlc import config

parser = argparse.ArgumentParser()
parser.add_argument("kind", choices=["issue", "dependency", "review"])
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--issue", default="free-shipping", help="prepared issue, for kind issue")
parser.add_argument("--ask", default=None, help="a comment to leave on the PR once the agent is listening")
parser.add_argument("--break-canary", action="store_true")
parser.add_argument("--hold", default="", help="comma-separated gates to leave waiting")
args = parser.parse_args()

http = httpx.Client(base_url=args.base, timeout=60)
REPLIES = {"plan": "/approve", "production": "/ship", "budget": "/continue", "clarify": "/skip"}


def comment(github_url: str, body: str) -> None:
    number = github_url.rsplit("/", 1)[-1]
    subprocess.run(["gh", "issue", "comment", number, "--repo", config.GITHUB_SANDBOX_REPO, "--body", body], check=True)


before = {r["run_id"] for r in http.get("/api/overview").json()["runs"]}
started = http.post("/api/scenarios", json={"kind": args.kind, "issue_id": args.issue})
started.raise_for_status()
scenario = started.json()
print("started on GitHub:", scenario["url"], flush=True)
if scenario["open_pr"]:
    # The bump: open the PR the presenter would open from GitHub's prefilled page.
    page = urlparse(scenario["url"])
    fields = {k: v[0] for k, v in parse_qs(page.query).items()}
    labels = [arg for label in fields["labels"].split(",") for arg in ("--label", label)]
    subprocess.run(["gh", "pr", "create", "--repo", config.GITHUB_SANDBOX_REPO, "--base", "main",
                    "--head", page.path.split("...", 1)[1], "--title", fields["title"], "--body", fields["body"],
                    *labels], check=True)

run_id = None
while run_id is None:  # the run appears when GitHub's webhook for it arrives
    time.sleep(2)
    run_id = next((r["run_id"] for r in http.get("/api/overview").json()["runs"] if r["run_id"] not in before), None)
print("run", run_id, flush=True)

seen: dict[str, tuple] = {}
answered: set[tuple] = set()
asked = broke = False
while True:
    time.sleep(3)
    view = http.get(f"/api/runs/{run_id}").json()
    run = view["run"]
    for s in run["steps"]:
        key = (s["status"], s["detail"])
        if seen.get(s["key"]) != key and s["status"] != "pending":
            seen[s["key"]] = key
            print(f"  [{s['status']:9}] {s['label']}: {s['detail'][:140]}", flush=True)
    for retry in view["retries"]:
        print(f"  retry: {retry['activity']} attempt {retry['attempt']}: {retry['last_failure'][:100]}")
    if args.break_canary and run.get("canary_sha") and not broke:
        http.post("/api/environments/break-canary", json={"seconds": 180})
        broke = True
        print("  ** broke the canary")
    gate = run.get("gate")
    if gate and (gate["key"], gate["waiting_since"]) not in answered and gate["key"] not in args.hold.split(","):
        answered.add((gate["key"], gate["waiting_since"]))
        target = gate["github_url"] or run["issue_url"] or run["pr_url"]
        if gate["key"] == "conversation":
            if args.ask and not asked:
                comment(target, args.ask)
                asked = True
                print(f"  commented on the PR: {args.ask}")
            else:
                subprocess.run(["gh", "pr", "close", target.rsplit("/", 1)[-1], "--repo", config.GITHUB_SANDBOX_REPO], check=True)
                print("  closed the PR")
        elif gate["key"] in REPLIES:
            comment(target, REPLIES[gate["key"]])
            print(f"  gate {gate['key']}: replied {REPLIES[gate['key']]}")
    if view["closed"]:
        print("FINAL", run["status"], f"${run['usage']['cost_usd']:.2f}", run["issue_url"], run["pr_url"], run.get("check"))
        for p in run["proofs"]:
            print("  proof", p["criterion"], p["detail"])
        break
