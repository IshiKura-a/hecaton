#!/usr/bin/env bash
#
# Production release: push HEAD, wait for the broker image to land on
# ghcr.io, pin .env to that tag, and run the full bootstrap so the
# cluster's broker + sandboxes + scaffolds all converge to this commit.
#
# Run from a clean tree on main:
#   make release
#
# Steps (each idempotent):
#   1. refuse if working tree is dirty or branch != main
#   2. `git push` if HEAD isn't on origin yet
#   3. dispatch broker-image.yml on HEAD and wait for it (every release
#      gets a freshly built broker image, even when nothing under
#      broker code paths changed)
#   4. rewrite .env: BROKER_IMAGE=ghcr.io/<owner>/hecaton-broker:sha-<full>
#   5. bash bootstrap/install.sh
#
# Note on phase 27 (stage scaffold tools): if any Sandbox CR is alive in
# the cluster, that phase fails by design rather than swapping mounts
# under a live sandbox. Release sandboxes first (provider.revoke or
# `kubectl delete sandbox -n hecaton-sandboxes --all`), then re-run.

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

# 3. trigger a broker-image build for this exact sha and wait for it.
# We always dispatch (rather than relying on the workflow's push-paths
# filter) so every release gets a freshly built image even when nothing
# under platform/broker/ or envs/ changed. Idempotent: if the same sha
# was already built, ghcr just gets the same digest re-pushed.
log "dispatching broker-image.yml for $short"
# Capture the wall-clock time just before dispatch so we can find the
# run we just created (gh has no direct dispatch -> run-id link).
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run broker-image.yml --ref "$sha"

log "looking up the dispatched run"
run_id=""
for attempt in $(seq 1 24); do
  run_id="$(gh run list \
    --workflow broker-image.yml \
    --event workflow_dispatch \
    --created ">=$since" \
    --limit 1 \
    --json databaseId,headSha \
    --jq ".[] | select(.headSha == \"$sha\") | .databaseId" 2>/dev/null || true)"
  [[ -n "$run_id" ]] && break
  log "  no dispatched run yet (attempt $attempt/24), sleeping 5s"
  sleep 5
done
[[ -n "$run_id" ]] || die "broker-image.yml dispatch never produced a run for $short"

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

# 5. full converge: every phase, idempotent.
log "running bootstrap/install.sh to converge fleet to $short"
bash "$HECATON_ROOT/bootstrap/install.sh"

log ""
log "release ok: cluster converged to $short (broker $new_image)"
