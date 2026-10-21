"""End-to-end check without the UI: start a run through the web API and approve every gate.

    uv run python scripts/drive_run.py [issue-id] [--supervised] [--break-canary] [--coder provider:model]
"""

import argparse
import secrets
import time

import httpx
import yaml

parser = argparse.ArgumentParser()
parser.add_argument("issue", nargs="?", default="free-shipping")
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--supervised", action="store_true")
parser.add_argument("--break-canary", action="store_true")
parser.add_argument("--coder", default="anthropic:claude-sonnet-5-5")
parser.add_argument("--reviewer", default="openai:gpt-5.5")
parser.add_argument("--subject", default=None, help="act as this presenter session (signed cookie)")
parser.add_argument("--hold", default="", help="comma-separated gates to leave waiting")
args = parser.parse_args()

issues = {i["id"]: i for i in yaml.safe_load(open("agentic_sdlc/issues.yaml"))}
issue = issues[args.issue]
cookies = {}
if args.subject:
    from itsdangerous import URLSafeSerializer
    from agentic_sdlc import config
    cookies["sdlc_session"] = URLSafeSerializer(config.SESSION_SECRET, salt="session").dumps(args.subject)
http = httpx.Client(base_url=args.base, timeout=30, cookies=cookies)
run_id = f"e2e-{secrets.token_hex(3)}"


def choice(key):
    provider, _, model = key.partition(":")
    return {"provider": provider, "model": model}


r = http.post("/api/runs", json={
    "run_id": run_id, "issue_title": issue["title"], "issue_body": issue["body"],
    "autonomy": "supervised" if args.supervised else "auto",
    "models": {"planner": choice("anthropic:claude-opus-5-5"), "coder": choice(args.coder), "reviewer": choice(args.reviewer)},
})
r.raise_for_status()
print("started", run_id, flush=True)
seen = {}
broke = False
while True:
    time.sleep(3)
    view = http.get(f"/api/runs/{run_id}").json()
    run = view["run"]
    for s in run["steps"]:
        key = (s["key"], s["status"], s["detail"])
        if seen.get(s["key"]) != key and s["status"] != "pending":
            seen[s["key"]] = key
            print(flush=True); print(f"  [{s['status']:9}] {s['label']}: {s['detail'][:140]}")
    for retry in view["retries"]:
        print(f"  retry: {retry['activity']} attempt {retry['attempt']}: {retry['last_failure'][:100]}")
    if args.break_canary and run.get("canary_sha") and not broke:
        http.post("/api/environments/break-canary", json={"seconds": 180}); broke = True
        print("  ** broke the canary")
    if run.get("gate") and run["gate"]["key"] not in args.hold.split(","):
        gate = run["gate"]["key"]
        print(f"  gate {gate}: approving")
        http.post(f"/api/runs/{run_id}/decide", json={"gate": gate, "approve": True, "raise_budget_usd": 3})
    if view["closed"]:
        print("FINAL", run["status"], f"${run['usage']['cost_usd']:.2f}", run["issue_url"], run["pr_url"])
        for p in run["proofs"]:
            print("  proof", p["criterion"], p["detail"])
        break
