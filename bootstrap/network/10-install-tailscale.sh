#!/usr/bin/env bash
#
# Install Tailscale on every host in config/hosts.yaml and join them to
# the tailnet identified by $TS_AUTHKEY. Idempotent: re-running is a no-op
# on hosts that are already up.
#
# Requires: .env with TS_AUTHKEY; config/hosts.yaml; working ssh access
# to every host (via ~/.ssh/config or ssh-agent).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

# shellcheck source=../../lib/common.sh
source "$HECATON_ROOT/lib/common.sh"
# shellcheck source=../../lib/inventory.sh
source "$HECATON_ROOT/lib/inventory.sh"
# shellcheck source=../../lib/remote.sh
source "$HECATON_ROOT/lib/remote.sh"

load_env
require_var TS_AUTHKEY

log "installing Tailscale on hosts: $(inventory_hosts | tr '\n' ' ')"

# Remote script. Runs on each host. Idempotent.
#   - installs tailscale via the official installer if missing
#   - calls `tailscale up` with the auth key
#   - `tailscale up` is itself idempotent: if already logged in with the
#     same args, it is effectively a no-op
remote_script=$(cat <<'REMOTE'
set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  echo "[remote] installing tailscale"
  curl -fsSL https://tailscale.com/install.sh | sudo sh
else
  echo "[remote] tailscale already installed: $(tailscale version | head -1)"
fi

# `--reset` makes `tailscale up` declarative: only the flags we pass apply.
# `--accept-routes=false` keeps host routing tables clean.
sudo tailscale up \
  --authkey="$TS_AUTHKEY_REMOTE" \
  --hostname="$(hostname | tr '[:upper:]' '[:lower:]')" \
  --accept-routes=false \
  --reset \
  $TS_EXTRA_ARGS_REMOTE

echo "[remote] tailscale status:"
tailscale status --self=true --peers=false
echo "[remote] tailscale ipv4: $(tailscale ip -4)"
REMOTE
)

env_prefix="TS_AUTHKEY_REMOTE=$(printf '%q' "$TS_AUTHKEY") TS_EXTRA_ARGS_REMOTE=$(printf '%q' "${TS_EXTRA_ARGS:-}")"

hosts=()
while IFS= read -r h; do hosts+=("$h"); done < <(inventory_hosts)
parallel_each_host "$env_prefix" "${hosts[@]}" <<< "$remote_script"

log "all hosts up. verify with:"
log "  tailscale status   # on your laptop"
log "next: bootstrap/cluster/20-install-k3s-server.sh"
