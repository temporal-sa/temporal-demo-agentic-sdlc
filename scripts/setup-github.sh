#!/usr/bin/env bash
# Set up the shared storefront repo for the GitHub-native demo. Runs as you (your gh login needs
# admin on the repo); safe to re-run. It:
#
#   1. creates the labels the demo uses;
#   2. lets the Dependency bump Action open pull requests;
#   3. puts sandbox-template/ on main and moves the baseline tag to it (lifting the rulesets that
#      lock them for a moment, and always restoring them);
#   4. pushes the review scenario's sample branch (sandbox-samples/discount-rounding);
#   5. requires the agent's check, agentic-sdlc/agent-review, on main, so GitHub shows it as a
#      Required check that stays pending until the agent is done (main stays locked: nothing merges);
#   6. with --webhook-url, points the repo's webhook at a hosted copy of the demo, signed with the
#      GITHUB_WEBHOOK_SECRET in the catalog's runtime secret.
#
#   scripts/setup-github.sh
#   scripts/setup-github.sh --webhook-url https://agentic-sdlc.tmprl-demo.cloud/webhooks/github
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO=$(pwd)

REPO=$(grep -E '^GITHUB_SANDBOX_REPO=' .env.local 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
REPO=${REPO:-temporal-sa/agentic-sdlc-storefront}
REGION=${AWS_REGION:-us-west-1}
RUNTIME_SECRET=tmprl-dem-cld/agentic-sdlc/runtime
CHECK=agentic-sdlc/agent-review
SAMPLE_BRANCH=samples/discount-rounding
EVENTS='["issues","issue_comment","pull_request","pull_request_review","pull_request_review_comment"]'

WEBHOOK_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --webhook-url) WEBHOOK_URL=$2; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

echo "Setting up $REPO"

# 1. Labels -----------------------------------------------------------------------------------
label() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null; echo "label $1"; }
label agent 1ff1a5 "Agents take on this issue"
label dependencies 0366d6 "Dependency updates"
label agent-plan 444ce7 "The planner agent posted a plan"
label rolled-back fecb2f "The canary failed and the change was reverted"

# 2. Actions may open PRs ------------------------------------------------------------------------
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true >/dev/null
echo "Actions can open pull requests"

# 3 and 4. main, baseline and the sample branch -------------------------------------------------
ruleset_id() { gh api "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$1\") | .id"; }
MAIN_RULES=$(ruleset_id protect-baseline)
TAG_RULES=$(ruleset_id protect-baseline-tag)
[[ -n "$MAIN_RULES" && -n "$TAG_RULES" ]] || { echo "Expected rulesets protect-baseline and protect-baseline-tag on $REPO" >&2; exit 1; }

enforce() { gh api -X PUT "repos/$REPO/rulesets/$1" -f enforcement="$2" >/dev/null; }
LIFTED=0
restore() { enforce "$MAIN_RULES" active; enforce "$TAG_RULES" active; LIFTED=0; echo "rulesets restored"; }
WORK=$(mktemp -d)
cleanup() { if [[ "$LIFTED" == 1 ]]; then restore; fi; rm -rf "$WORK"; }
trap cleanup EXIT  # the rulesets go back on even if a push fails
gh repo clone "$REPO" "$WORK/repo" -- --quiet
cd "$WORK/repo"
git config user.name "agentic-sdlc setup"
git config user.email "agentic-sdlc-setup@users.noreply.github.com"

rsync -a --delete --exclude .git --exclude node_modules "$DEMO/sandbox-template/" ./
git add -A
UPDATED_MAIN=0
if ! git diff --cached --quiet; then
  git commit -qm "Storefront template: orbit-money, the Dependency bump Action"
  UPDATED_MAIN=1
fi

if [[ "$UPDATED_MAIN" == 1 || "$(git rev-parse HEAD)" != "$(git rev-parse baseline 2>/dev/null || true)" ]]; then
  LIFTED=1
  enforce "$MAIN_RULES" disabled; enforce "$TAG_RULES" disabled
  git push -q origin HEAD:main
  git tag -f baseline HEAD >/dev/null
  git push -q -f origin refs/tags/baseline
  restore
  echo "main and baseline at $(git rev-parse --short HEAD)"
else
  echo "main and baseline already match sandbox-template/"
fi

git switch -q -C "$SAMPLE_BRANCH"
cp -R "$DEMO/sandbox-samples/discount-rounding/src" "$DEMO/sandbox-samples/discount-rounding/tests" ./
git add -A
git commit -qm "Add a bulk discount for 3 or more items"
git push -q -f origin "$SAMPLE_BRANCH"
echo "sample branch $SAMPLE_BRANCH pushed"
cd "$DEMO"

# 5. Require the agent's check on main --------------------------------------------------------------
RULES=$(cat <<JSON
{"rules": [
  {"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"},
  {"type": "required_status_checks", "parameters": {
    "strict_required_status_checks_policy": false,
    "required_status_checks": [{"context": "$CHECK"}]}}
]}
JSON
)
gh api -X PUT "repos/$REPO/rulesets/$MAIN_RULES" --input - <<<"$RULES" >/dev/null
echo "main requires $CHECK"

# 6. The hosted demo's webhook --------------------------------------------------------------------------
if [[ -n "$WEBHOOK_URL" ]]; then
  SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$RUNTIME_SECRET" \
    --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["GITHUB_WEBHOOK_SECRET"])')
  HOOK=$(python3 -c 'import json,sys; print(json.dumps({"active": True, "events": json.loads(sys.argv[3]),
    "config": {"url": sys.argv[1], "content_type": "json", "secret": sys.argv[2], "insecure_ssl": "0"}}))' \
    "$WEBHOOK_URL" "$SECRET" "$EVENTS")
  EXISTING=$(gh api "repos/$REPO/hooks" --jq ".[] | select(.config.url == \"$WEBHOOK_URL\") | .id")
  if [[ -n "$EXISTING" ]]; then
    gh api -X PATCH "repos/$REPO/hooks/$EXISTING" --input - <<<"$HOOK" >/dev/null
    echo "webhook $WEBHOOK_URL updated"
  else
    gh api -X POST "repos/$REPO/hooks" --input - <<<"$HOOK" >/dev/null
    echo "webhook $WEBHOOK_URL created"
  fi
fi

echo "Done."
