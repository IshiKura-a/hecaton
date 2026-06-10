#!/usr/bin/env bash
#
# Unified dev iteration. Detects what changed since last run and
# updates exactly that — scaffolds (rsync to fleet), broker (build +
# import + rolling deploy), trainer base image (rebuild on the
# trainer host), then optionally runs the smoke script.
#
# Usage:
#   dev/iter.sh                    deploy phases only, no smoke
#   dev/iter.sh host=<ssh-alias>   also rsync sources + run smoke
#   dev/iter.sh host=<alias> smoke=<script>
#                                  pick smoke script under
#                                  examples/trainer-smoke/ (default:
#                                  run_r2egym.py)
#   HOST=<ssh-alias> dev/iter.sh   same; env-var form
#
# Env knobs:
#   ONLY=<phase>                   run a single phase
#                                  (scaffold | broker | trainer-image | smoke)
#   SKIP_SCAFFOLD=1                skip a phase
#   SKIP_BROKER=1
#   SKIP_TRAINER_IMAGE=1
#   SKIP_SMOKE=1
#   FORCE_SCAFFOLD=1               re-run even when content hash matches
#   FORCE_BROKER=1
#   FORCE_TRAINER_IMAGE=1
#
# State cache:
#   .cache/dev-iter/<phase>.hash   last-deployed content hash. Wipe
#                                  the dir to force-redo every phase
#                                  (or use FORCE_*).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/version.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"
load_env

STATE_DIR="$HECATON_ROOT/.cache/dev-iter"
mkdir -p "$STATE_DIR"

# host= positional argument wins over $HOST env.
HOST="${HOST:-}"
SMOKE="${SMOKE:-run_r2egym.py}"
for arg in "$@"; do
  case "$arg" in
    host=*) HOST="${arg#host=}" ;;
    smoke=*) SMOKE="${arg#smoke=}" ;;
    *) die "unknown arg: $arg (expected host=<ssh-alias> or smoke=<script>)" ;;
  esac
done

# Validate smoke script exists locally (it'll be rsync'd to the host).
if [[ ! -f "$HECATON_ROOT/examples/trainer-smoke/$SMOKE" ]]; then
  die "smoke script not found: examples/trainer-smoke/$SMOKE"
fi

ONLY="${ONLY:-}"

# Resolve the broker URL by ssh'ing to the role:server host and asking
# its tailscaled. Cached so we don't pay the ssh round-trip every run;
# wipe .cache/dev-iter/broker-url if the server's tailnet IP changes.
broker_url() {
  local cache="$STATE_DIR/broker-url"
  if [[ -f "$cache" ]]; then
    cat "$cache"
    return
  fi
  local server=""
  for h in $(inventory_hosts); do
    if [[ "$(inventory_field "$h" role)" == "server" ]]; then
      server="$h"; break
    fi
  done
  [[ -n "$server" ]] || die "no role:server host in $HECATON_ROOT/config/hosts.yaml"
  local ip
  ip="$(ssh_to "$server" 'tailscale ip -4 2>/dev/null | head -1' | tr -d '[:space:]')"
  [[ -n "$ip" ]] || die "could not get tailnet ip from $server (is tailscaled running there?)"
  local url="http://${ip}:30443"
  printf '%s' "$url" > "$cache"
  log "resolved broker URL: $url (cached at $cache)"
  printf '%s' "$url"
}

# Pre-flight: if we'll need to run smoke, fail fast on missing env now
# rather than after a 3-minute trainer-image build.
if [[ -n "$HOST" && -z "${SKIP_SMOKE:-}" && ( -z "$ONLY" || "$ONLY" == "smoke" ) ]]; then
  require_var TS_AUTHKEY_TRAINER
  require_var HECATON_TOKEN
  HECATON_BROKER_URL="$(broker_url)"
fi

# --- helpers --------------------------------------------------------------

# Return 0 if the named phase should run, 1 if it should be skipped.
want() {
  local name="$1"
  if [[ -n "$ONLY" && "$ONLY" != "$name" ]]; then return 1; fi
  local skip_var
  skip_var="SKIP_$(echo "$name" | tr '[:lower:]-' '[:upper:]_')"
  if [[ -n "${!skip_var:-}" ]]; then
    log "$name: skipped (\$$skip_var set)"
    return 1
  fi
  return 0
}

_kubectl() {
  KUBECONFIG="$HECATON_ROOT/config/kubeconfig" kubectl "$@"
}

# rsync the repo to $HOST:/tmp/hecaton/. Excludes everything that's
# either user-specific (config/) or build noise (.git, caches).
rsync_repo() {
  [[ -n "$HOST" ]] || die "rsync_repo: HOST unset"
  rsync -az --delete \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='.cache/' \
    --exclude='.ruff_cache/' \
    --exclude='config/' \
    --exclude='*.pyc' \
    -e 'ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new' \
    "$HECATON_ROOT/" "$HOST:/tmp/hecaton/"
}

_host_ssh() {
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$HOST" "$@"
}

# --- phases ---------------------------------------------------------------

step_scaffold() {
  want scaffold || return 0
  local cur stamp
  cur="$(hash_paths scaffolds)"
  stamp="$STATE_DIR/scaffold.hash"
  if [[ -z "${FORCE_SCAFFOLD:-}" && -f "$stamp" && "$(cat "$stamp")" == "$cur" ]]; then
    log "scaffold: unchanged (hash ${cur:0:12}), skip"
    return 0
  fi
  log "scaffold: staging to fleet (hash ${cur:0:12})"
  bash "$HECATON_ROOT/bootstrap/cluster/26-stage-agent-tools.sh"
  echo "$cur" > "$stamp"
}

step_broker() {
  want broker || return 0

  local cur tag
  cur="$(hash_paths platform/broker)"
  tag="docker.io/library/hecaton-broker:dev-${cur:0:12}"

  local cur_image=""
  if [[ -f "$HECATON_ROOT/config/kubeconfig" ]]; then
    cur_image="$(_kubectl -n hecaton-system get deploy hecaton-broker \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  fi

  if [[ -z "${FORCE_BROKER:-}" && "$cur_image" == "$tag" ]]; then
    log "broker: cluster already running $tag, skip"
    return 0
  fi

  log "broker: build + import + deploy $tag"
  log "  (cluster currently: ${cur_image:-<none>})"
  BROKER_IMAGE="$tag" bash "$HECATON_ROOT/platform/broker/build-and-import.sh"

  if [[ -n "$cur_image" ]]; then
    _kubectl -n hecaton-system set image deploy/hecaton-broker broker="$tag"
    _kubectl -n hecaton-system rollout status deploy/hecaton-broker --timeout=120s
  else
    log "broker: no Deployment yet — run bash bootstrap/cluster/28-install-broker.sh"
    log "  (set BROKER_IMAGE=$tag in .env first, or pass it inline)"
  fi
}

step_trainer_image() {
  want trainer-image || return 0
  if [[ -z "$HOST" ]]; then
    log "trainer-image: no host=, skip"
    return 0
  fi

  local cur stamp
  cur="$(hash_paths examples/trainer-smoke/Dockerfile envs/trainer-entrypoint.sh)"
  stamp="$STATE_DIR/trainer-image.$HOST.hash"

  # Probe whether the image even exists on the host — if not, rebuild
  # regardless of stamp (covers first-run + image-deleted-by-hand).
  local has_image=1
  _host_ssh "docker image inspect hecaton-trainer:base >/dev/null 2>&1" || has_image=0

  if [[ -z "${FORCE_TRAINER_IMAGE:-}" && "$has_image" == "1" \
        && -f "$stamp" && "$(cat "$stamp")" == "$cur" ]]; then
    log "trainer-image: $HOST already at hash ${cur:0:12}, skip"
    return 0
  fi

  log "trainer-image: rebuilding hecaton-trainer:base on $HOST (hash ${cur:0:12})"
  rsync_repo
  _host_ssh "cd /tmp/hecaton && docker build \
    -f examples/trainer-smoke/Dockerfile \
    --build-arg HECATON_SOURCE=mount \
    --build-arg INCLUDE_R2EGYM=true \
    -t hecaton-trainer:base ."
  echo "$cur" > "$stamp"
}

step_smoke() {
  want smoke || return 0
  if [[ -z "$HOST" ]]; then
    log "smoke: no host=, skip (pass host=<ssh-alias> to run)"
    return 0
  fi

  require_var TS_AUTHKEY_TRAINER
  require_var HECATON_TOKEN
  local broker
  broker="$(broker_url)"

  local run_id="${HECATON_RUN_ID:-dev-$(whoami)-$(date +%s)}"

  # In ONLY=smoke mode we may have skipped the rsync from trainer-image,
  # so make sure /tmp/hecaton on the host is current.
  log "smoke: rsync sources to $HOST"
  rsync_repo

  # Run the container as the calling user so anything it writes to the
  # bind mount (pip's egg-info) is owned by that user, not root — next
  # rsync's --delete can then clean it up. tailscaled wants /var/run
  # writable, so we redirect HOME to /tmp.
  local host_uid host_gid
  host_uid="$(_host_ssh 'id -u' | tr -d '[:space:]')"
  host_gid="$(_host_ssh 'id -g' | tr -d '[:space:]')"

  log "smoke: running $SMOKE on $HOST (run_id=$run_id, broker=$broker, uid=$host_uid)"
  _host_ssh "docker run --rm \
    --user $host_uid:$host_gid \
    -v /tmp/hecaton:/hecaton-src \
    -e HOME=/tmp \
    -e TS_STATE_DIR=/tmp/tailscale-state \
    -e TS_RUN_DIR=/tmp/tailscale-run \
    -e HECATON_SDK_PATH=/hecaton-src/envs \
    -e TS_AUTHKEY='$TS_AUTHKEY_TRAINER' \
    -e HECATON_BROKER_URL='$broker' \
    -e HECATON_TOKEN='$HECATON_TOKEN' \
    -e HECATON_RUN_ID='$run_id' \
    hecaton-trainer:base \
    python /hecaton-src/examples/trainer-smoke/$SMOKE"
}

# --- run ------------------------------------------------------------------

step_scaffold
step_broker
step_trainer_image
step_smoke

log "dev iter done."
