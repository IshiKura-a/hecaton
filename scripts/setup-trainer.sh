#!/usr/bin/env bash
#
# Trainer-side bootstrap. Run this ON the trainer host. The script is
# self-contained — no laptop needed once the file is on the host.
#
# Required env (export before running, or put in a sourced file):
#   TS_AUTHKEY_TRAINER     Tailscale auth key with tag:trainer
#   HECATON_BROKER_URL     e.g. http://<gcr-host-tailnet-ip>:30443
#   HECATON_TOKEN          shared bearer token
#   HECATON_RUN_ID         identifier for this RL run (used to revoke
#                          orphan sandboxes if the trainer restarts)
#
# What it does:
#   1. install tailscale (userspace mode, no host kernel deps)
#   2. join the hecaton tailnet with tag:trainer and --accept-routes
#      so we can reach sandbox pod IPs (10.42.0.0/16)
#   3. install the hecaton_envs Python SDK from PyPI/git
#   4. print the env block you need to source to start using the SDK
#
# Idempotent. Re-running is safe.

set -euo pipefail

: "${TS_AUTHKEY_TRAINER:?TS_AUTHKEY_TRAINER is required (Tailscale auth key with tag:trainer)}"
: "${HECATON_BROKER_URL:?HECATON_BROKER_URL is required (e.g. http://100.x.x.x:30443)}"
: "${HECATON_TOKEN:?HECATON_TOKEN is required (shared bearer token)}"
: "${HECATON_RUN_ID:?HECATON_RUN_ID is required (an identifier for this RL run)}"

TS_HOSTNAME="${TS_HOSTNAME:-trainer-$(hostname)-$$}"
TS_SOCKS_PORT="${TS_SOCKS_PORT:-1055}"
TS_SOCK="${TS_SOCK:-/tmp/hecaton-tailscaled.sock}"
TS_STATE="${TS_STATE:-/tmp/hecaton-tailscaled.state}"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

# 1. tailscale binary
if ! command -v tailscale >/dev/null 2>&1; then
  log "installing tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# 2. start userspace tailscaled if not already running. We use unique
# socket/state paths under /tmp so we don't conflict with any
# host-level tailscaled the trainer host may already run.
if ! tailscale --socket="$TS_SOCK" status >/dev/null 2>&1; then
  log "starting tailscaled (userspace mode)"
  tailscaled \
    --tun=userspace-networking \
    --socks5-server="localhost:${TS_SOCKS_PORT}" \
    --socket="$TS_SOCK" \
    --state="$TS_STATE" \
    >/tmp/hecaton-tailscaled.log 2>&1 &

  for _ in $(seq 1 20); do
    tailscale --socket="$TS_SOCK" status >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

# 3. join the tailnet (idempotent: --reset replays exactly the flags below)
log "tailscale up (--accept-routes for sandbox pod CIDR)"
tailscale --socket="$TS_SOCK" up \
  --authkey="$TS_AUTHKEY_TRAINER" \
  --hostname="$TS_HOSTNAME" \
  --accept-routes \
  --reset

ts_ip="$(tailscale --socket="$TS_SOCK" ip -4 | head -1)"
log "trainer tailnet ip: $ts_ip"

# 4. install the SDK editable so `git pull` is enough to pick up
# changes — no re-running this script after every upstream bump.
# Normal path: trainer git-cloned this repo, HECATON_SDK_PATH=$PWD/envs.
# Fallback (no local checkout): pip install straight from GitHub; that
# install is a snapshot, so upgrading then needs another pip install.
if [[ -d "${HECATON_SDK_PATH:-}" ]]; then
  log "installing hecaton-envs (editable) from $HECATON_SDK_PATH"
  pip install --user -q -e "$HECATON_SDK_PATH"
else
  log "installing hecaton-envs from git (fallback; trainer must reach GitHub)"
  pip install --user -q "git+https://github.com/IshiKura-a/hecaton.git#subdirectory=envs" \
    || die "failed to install hecaton-envs; set HECATON_SDK_PATH to a local checkout"
fi

# 5. print the env block trainer code needs. The SDK reads
# HECATON_BROKER_URL + HECATON_TOKEN from environ. Network traffic to
# the broker / sandbox pods must go through tailscaled — so the SOCKS5
# proxy must be exported.
cat <<EOF
==============================================================
trainer setup ok.

Export these in the shell that will run your trainer process:

  export ALL_PROXY="socks5h://localhost:${TS_SOCKS_PORT}"
  export HTTPS_PROXY="\$ALL_PROXY"
  export HTTP_PROXY="\$ALL_PROXY"
  export NO_PROXY="localhost,127.0.0.1"

  export HECATON_BROKER_URL=$HECATON_BROKER_URL
  export HECATON_TOKEN=$HECATON_TOKEN
  export HECATON_RUN_ID=$HECATON_RUN_ID

Quick sanity check:

  python -c "from hecaton_envs import SandboxProvider; p = SandboxProvider.from_env(run_id='$HECATON_RUN_ID'); print(p.revoke())"

==============================================================
EOF
