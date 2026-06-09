#!/usr/bin/env bash
#
# Production broker release: push HEAD, wait for the CI image build,
# pin .env to the resulting ghcr.io tag, and redeploy the broker.
#
# Run from a clean tree on main:
#   make release
#
# What it does (idempotent at each step):
#   1. refuse if working tree is dirty or branch != main
#   2. `git push` if HEAD isn't on origin yet
#   3. `gh run watch` the broker-image.yml run for this sha until success
#   4. rewrite .env: BROKER_IMAGE=ghcr.io/<owner>/hecaton-broker:sha-<full>
#   5. bash bootstrap/cluster/26-install-broker.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/version.sh"

require_cmd git
require_cmd gh

cd "$HECATON_ROOT"

# 1. clean tree on main
branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || die "must be on main branch (current: $branch)"
[[ -z "$(git_dirty)" ]] || die "working tree is dirty; commit or stash first"

sha="$(git_sha)"
short="$(git_sha_short)"

# 2. push if HEAD isn't on origin yet
if ! git merge-base --is-ancestor "$sha" "origin/$branch" 2>/dev/null; then
  log "pushing $short to origin/$branch"
  git push origin "$branch"
else
  log "$short already on origin/$branch"
fi

# 3. find or wait for the broker-image workflow run on this sha
log "looking up broker-image.yml run for $short"
run_id=""
for attempt in $(seq 1 12); do
  run_id="$(gh run list \
    --workflow broker-image.yml \
    --branch "$branch" \
    --commit "$sha" \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId // empty' 2>/dev/null || true)"
  [[ -n "$run_id" ]] && break
  log "  no run yet for $short (attempt $attempt/12), sleeping 5s"
  sleep 5
done
[[ -n "$run_id" ]] || die "broker-image.yml never started for $short — did the path filter match? (see .github/workflows/broker-image.yml)"

log "watching run $run_id"
gh run watch "$run_id" --exit-status
log "CI build succeeded"

# 4. pin .env
owner="$(gh repo view --json owner --jq '.owner.login' | tr '[:upper:]' '[:lower:]')"
new_image="ghcr.io/${owner}/hecaton-broker:sha-${sha}"
log "pinning BROKER_IMAGE=$new_image in .env"

env_file="$HECATON_ROOT/.env"
[[ -f "$env_file" ]] || die "missing $env_file"
# Match commented + uncommented forms. Use a sentinel so we don't
# accidentally substring-match another var.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
awk -v new="BROKER_IMAGE=$new_image" '
  /^[[:space:]]*#?[[:space:]]*BROKER_IMAGE=/ { print new; replaced=1; next }
  { print }
  END { if (!replaced) print new }
' "$env_file" > "$tmp"
mv "$tmp" "$env_file"

# 5. redeploy
log "redeploying broker"
bash "$HECATON_ROOT/bootstrap/cluster/26-install-broker.sh"

log ""
log "release ok: broker now running $new_image"
