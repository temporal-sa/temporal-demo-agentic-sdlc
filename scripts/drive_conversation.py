"""Rehearse the steering beats end to end, against real agents and the real sandbox repo.

    uv run python scripts/drive_conversation.py

It starts a run on a deliberately open-ended issue, then plays a developer: answers the planner's
questions, messages a coder while it works, comments on the PR from GitHub (with your `gh` CLI)
and finally asks for a new requirement in the console, which sends the run back to planning.
Every step prints what the run did with it.
"""

import argparse
import json
import secrets
import subprocess
import time

import httpx
from itsdangerous import URLSafeSerializer

from agentic_sdlc import config

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--subject", default="conversation-rehearsal")
parser.add_argument("--skip-scope-change", action="store_true")
args = parser.parse_args()

cookie = URLSafeSerializer(config.SESSION_SECRET, salt="session").dumps(args.subject)
http = httpx.Client(base_url=args.base, timeout=30, cookies={"sdlc_session": cookie})
run_id = f"talk-{secrets.token_hex(3)}"

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


def post(path: str, body: dict) -> None:
    r = http.post(f"/api/runs/{run_id}/{path}", json=body)
    if r.status_code >= 400:
        raise SystemExit(f"{path} refused: {r.text}")


def show_conversation(v: dict) -> None:
    for f in v["run"]["conversation"]:
        print(f"    [{f['source']:7}] {f['author']}: {f['text'][:70]!r} → {f['kind']} / {f['status']} / {f['outcome'][:80]}")


http.post("/api/runs", json={"run_id": run_id, "issue_title": ISSUE["title"], "issue_body": ISSUE["body"],
                             "autonomy": "auto"}).raise_for_status()
print("started", run_id)

v = wait_for(lambda v: gate(v) in ("clarify", "plan"), "the planner")
if gate(v) == "clarify":
    print("  planner asked:", *v["run"]["gate"]["questions"], sep="\n    - ")
    post("decide", {"gate": "clarify", "approve": True,
                    "feedback": "Show saved items in their own list under the cart, with a Move to cart button. "
                                "Saved items don't count towards the totals."})
    v = wait_for(lambda v: gate(v) == "plan", "the plan")
print("  plan:", v["run"]["plan"]["summary"][:160])
post("decide", {"gate": "plan", "approve": True})

v = wait_for(lambda v: any(s["lane"] and s["status"] == "active" for s in v["run"]["steps"]), "a coder")
lane = next(s["key"] for s in v["run"]["steps"] if s["lane"] and s["status"] == "active")
post("steer", {"lane": lane, "text": "Keep the saved-items logic pure and tested, like the rest of cart.js."})
print("  steered", lane)

v = wait_for(lambda v: gate(v) == "merge", "the review window")
pr_url = v["run"]["pr_url"]
number = pr_url.rsplit("/", 1)[-1]
print("  PR:", pr_url)
subprocess.run(["gh", "pr", "comment", number, "--repo", config.GITHUB_SANDBOX_REPO, "--body",
                "The Move to cart button should be labelled 'Move to cart', not just an icon."], check=True)
print("  commented on GitHub; waiting for the agents to answer")
v = wait_for(lambda v: any(f["source"] == "github" and f["status"] == "addressed" for f in v["run"]["conversation"]),
             "the PR comment to be addressed")
show_conversation(v)

if not args.skip_scope_change:
    v = wait_for(lambda v: gate(v) == "merge", "the review window again")
    post("comment", {"text": "Also show how many items are saved in the header, next to the cart count."})
    print("  asked for a new requirement in the console")
    v = wait_for(lambda v: gate(v) == "plan" and v["run"]["plan_revision"] == 2, "the revised plan")
    print("  revision 2 plan:", v["run"]["plan"]["summary"][:160])
    post("decide", {"gate": "plan", "approve": True})

v = wait_for(lambda v: gate(v) == "production", "the production gate", timeout=1800)
post("decide", {"gate": "production", "approve": True})
v = wait_for(lambda v: v["closed"], "the end", timeout=900)
print("FINAL", v["run"]["status"], f"${v['run']['usage']['cost_usd']:.2f}", v["run"]["issue_url"], v["run"]["pr_url"])
show_conversation(v)
for p in v["run"]["proofs"]:
    print("  proof", p["criterion"], p["detail"])
print(json.dumps({"run": run_id}))
