"""Rehearse the steering beats end to end, against real agents and the real sandbox repo.

    uv run python scripts/drive_conversation.py

It opens a deliberately open-ended issue through the console's "Start a scenario", then plays a
developer entirely on GitHub with your `gh` CLI: answers the planner's questions on the issue,
replies /approve, steers the coders with an issue comment while they work, comments on the PR,
asks for a new requirement on the PR (which sends the run back to planning) and replies /ship.
Every step prints what the run did with it.
"""

import argparse
import json
import subprocess
import time

import httpx

from agentic_sdlc import config

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--skip-scope-change", action="store_true")
args = parser.parse_args()

http = httpx.Client(base_url=args.base, timeout=60)
run_id = ""

ISSUE = {
    "title": "Let customers save items for later",
    "body": "Customers want to move cart items to a 'saved for later' list instead of deleting them.",
}


def view() -> dict:
    # The run appears a moment after it starts; until then the API answers 404.
    while (r := http.get(f"/api/runs/{run_id}")).status_code == 404:
        time.sleep(1)
    return r.json()


def wait_for(predicate, what: str, timeout: float = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = view()
        if predicate(v):
            return v
        if v["closed"]:
            raise SystemExit(f"run ended while waiting for {what}: {v['run']['status']}")
        time.sleep(3)
    raise SystemExit(f"timed out waiting for {what}")


def gate(v: dict) -> str | None:
    return (v["run"]["gate"] or {}).get("key")


def comment(url: str, body: str) -> None:
    """Comment on the run's issue or PR, as a person would."""
    subprocess.run(["gh", "issue", "comment", url.rsplit("/", 1)[-1], "--repo", config.GITHUB_SANDBOX_REPO,
                    "--body", body], check=True)


def show_conversation(v: dict) -> None:
    for f in v["run"]["conversation"]:
        print(f"    @{f['author']}: {f['text'][:70]!r} → {f['kind']} / {f['status']} / {f['outcome'][:80]}")


before = {r["run_id"] for r in http.get("/api/overview").json()["runs"]}
http.post("/api/scenarios", json={"kind": "issue", "title": ISSUE["title"], "body": ISSUE["body"],
                                  "settings": {**http.get("/api/config").json()["settings"], "autonomy": "auto"}}).raise_for_status()
while not run_id:  # the run appears when GitHub's webhook for the new issue arrives
    time.sleep(2)
    run_id = next((r["run_id"] for r in http.get("/api/overview").json()["runs"] if r["run_id"] not in before), "")
print("started", run_id)

v = wait_for(lambda v: gate(v) in ("clarify", "plan"), "the planner")
issue_url = v["run"]["issue_url"]
if gate(v) == "clarify":
    print("  planner asked:", *v["run"]["gate"]["questions"], sep="\n    - ")
    comment(issue_url, "Show saved items in their own list under the cart, with a Move to cart button. "
                       "Saved items don't count towards the totals.")
    v = wait_for(lambda v: gate(v) == "plan", "the plan")
print("  plan:", v["run"]["plan"]["summary"][:160])
comment(issue_url, "/approve")

v = wait_for(lambda v: any(s["lane"] and s["status"] == "active" for s in v["run"]["steps"]), "a coder")
comment(issue_url, "Keep the saved-items logic pure and tested, like the rest of cart.js.")
print("  steered the coders from the issue")

v = wait_for(lambda v: gate(v) == "merge", "the review window")
pr_url = v["run"]["pr_url"]
print("  PR:", pr_url)
comment(pr_url, "The Move to cart button should be labelled 'Move to cart', not just an icon.")
print("  commented on the PR; waiting for the agents to answer")
v = wait_for(lambda v: any(f["kind"] == "feedback" and f["status"] == "addressed" for f in v["run"]["conversation"]),
             "the PR comment to be addressed")
show_conversation(v)

if not args.skip_scope_change:
    v = wait_for(lambda v: gate(v) == "merge", "the review window again")
    comment(v["run"]["pr_url"], "Also show how many items are saved in the header, next to the cart count.")
    print("  asked for a new requirement on the PR")
    v = wait_for(lambda v: gate(v) == "plan" and v["run"]["plan_revision"] == 2, "the revised plan")
    print("  revision 2 plan:", v["run"]["plan"]["summary"][:160])
    comment(issue_url, "/approve")

v = wait_for(lambda v: gate(v) == "production", "the production gate", timeout=1800)
comment(issue_url, "/ship")
v = wait_for(lambda v: v["closed"], "the end", timeout=900)
print("FINAL", v["run"]["status"], f"${v['run']['usage']['cost_usd']:.2f}", v["run"]["issue_url"], v["run"]["pr_url"])
show_conversation(v)
for p in v["run"]["proofs"]:
    print("  proof", p["criterion"], p["detail"])
print(json.dumps({"run": run_id}))
