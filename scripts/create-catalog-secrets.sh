#!/usr/bin/env bash
# Create (or update) the project-owned secrets the agentic-sdlc DemoProject needs, in the
# catalog's AWS account and region. Reads GITHUB_TOKEN from .env.local, generates the runtime
# values, and never prints a secret. Safe to re-run: existing secrets get a new version.
#
#   scripts/create-catalog-secrets.sh            # create both
#   ROTATE_RUNTIME=0 scripts/create-catalog-secrets.sh   # update only the GitHub token
#
# The shared anthropic, openai and gemini secrets already exist under tmprl-dem-cld/platform/shared/.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=${AWS_REGION:-us-west-1}
PREFIX=tmprl-dem-cld/agentic-sdlc
ROTATE_RUNTIME=${ROTATE_RUNTIME:-1}

GITHUB_TOKEN=$(grep -E '^GITHUB_TOKEN=' .env.local | cut -d= -f2- | tr -d '[:space:]')
[[ -n "$GITHUB_TOKEN" ]] || { echo "GITHUB_TOKEN is missing from .env.local" >&2; exit 1; }

upsert() {  # name, json, description
  if aws secretsmanager describe-secret --region "$REGION" --secret-id "$1" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --region "$REGION" --secret-id "$1" --secret-string "$2" >/dev/null
    echo "updated $1"
  else
    aws secretsmanager create-secret --region "$REGION" --name "$1" --description "$3" \
      --secret-string "$2" --tags Key=demo,Value=agentic-sdlc >/dev/null
    echo "created $1"
  fi
}

upsert "$PREFIX/github" \
  "$(python3 -c 'import json,sys; print(json.dumps({"GITHUB_TOKEN": sys.argv[1]}))' "$GITHUB_TOKEN")" \
  "Fine-grained PAT for temporal-sa/agentic-sdlc-storefront (contents, PRs, issues read/write). Expires 2027-10-22."

if [[ "$ROTATE_RUNTIME" == "1" ]]; then
  upsert "$PREFIX/runtime" \
    "$(python3 -c 'import json,secrets; print(json.dumps({"REDIS_PASSWORD": secrets.token_urlsafe(32), "SESSION_SECRET": secrets.token_urlsafe(48)}))')" \
    "Redis password and session-cookie signing secret for the agentic-sdlc demo."
fi

# Show only the key names, to confirm the shape the DemoProject requires.
for s in github runtime; do
  printf '%-8s ' "$s"
  aws secretsmanager get-secret-value --region "$REGION" --secret-id "$PREFIX/$s" --query SecretString --output text \
    | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)))'
done
