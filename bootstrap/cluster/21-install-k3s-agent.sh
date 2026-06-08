#!/usr/bin/env bash
#
# Install or verify the k3s agent on every host with `role: agent` in
# config/hosts.yaml, joining the server installed by
# 20-install-k3s-server.sh. Pins the version from lib/k3s-version.sh.
# Idempotent: hosts already running the pinned agent are skipped.
#
# Requires config/k3s-node-token and config/kubeconfig (produced by 20-).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"
source "$HECATON_ROOT/lib/k3s-version.sh"

token_file="$HECATON_ROOT/config/k3s-node-token"
kc_file="$HECATON_ROOT/config/kubeconfig"
[[ -f "$token_file" ]] || die "missing $token_file (run 20-install-k3s-server.sh first)"
[[ -f "$kc_file"    ]] || die "missing $kc_file (run 20-install-k3s-server.sh first)"

node_token="$(cat "$token_file")"
server_url="$(awk '/server:/{print $2; exit}' "$kc_file")"
[[ -n "$server_url" ]] || die "could not parse server URL from $kc_file"

agents=()
for h in $(inventory_hosts); do
  [[ "$(inventory_field "$h" role)" == "agent" ]] && agents+=("$h")
done
[[ ${#agents[@]} -gt 0 ]] || die "no host with role: agent in $(inventory_path)"

force="${HECATON_FORCE:-}"

log "k3s $K3S_VERSION agents: ${agents[*]}  joining $server_url"

remote_script=$(cat <<'REMOTE'
set -euo pipefail
WANT="$K3S_VERSION_REMOTE"

ts_ip="$(tailscale ip -4 | head -1)"
[[ -n "$ts_ip" ]] || { echo "no tailscale ipv4 on this host" >&2; exit 1; }

current=""
if command -v k3s >/dev/null 2>&1; then
  current="$(k3s --version 2>/dev/null | head -1 | awk '{print $3}')"
fi

if [[ "$current" == "$WANT" ]] && [[ -z "${FORCE_REMOTE:-}" ]] && systemctl is-active --quiet k3s-agent; then
  echo "[remote] k3s agent $WANT already running on $(hostname) (use --force to reconfigure)"
  exit 0
fi

if [[ -n "$current" && "$current" != "$WANT" ]]; then
  echo "[remote] upgrading k3s $current -> $WANT"
else
  echo "[remote] installing k3s agent $WANT"
fi

# Default max-pods to CPU count if not configured.
max_pods="${MAX_PODS_REMOTE:-$(nproc)}"

curl -sfL https://get.k3s.io | \
  INSTALL_K3S_VERSION="$WANT" \
  K3S_URL="$K3S_URL_REMOTE" \
  K3S_TOKEN="$K3S_TOKEN_REMOTE" \
  INSTALL_K3S_EXEC="agent \
    --flannel-iface=tailscale0 \
    --node-ip=$ts_ip \
    --kubelet-arg=max-pods=$max_pods \
    --kubelet-arg=image-gc-high-threshold=85 \
    --kubelet-arg=image-gc-low-threshold=70 \
    --kubelet-arg=serialize-image-pulls=false \
    --kubelet-arg=max-parallel-image-pulls=16 \
    --kubelet-arg=eviction-hard= \
    --kubelet-arg=eviction-soft=" \
  sh -

systemctl is-active --quiet k3s-agent \
  || { echo "k3s-agent failed to start" >&2; exit 1; }
REMOTE
)

for h in "${agents[@]}"; do
  max_pods="$(inventory_field "$h" max_pods 2>/dev/null || true)"
  env_prefix="K3S_VERSION_REMOTE=$(printf '%q' "$K3S_VERSION") K3S_URL_REMOTE=$(printf '%q' "$server_url") K3S_TOKEN_REMOTE=$(printf '%q' "$node_token") MAX_PODS_REMOTE=$(printf '%q' "$max_pods") FORCE_REMOTE=$(printf '%q' "$force")"
  log "installing agent on $h (max_pods=${max_pods:-auto/nproc})"
  ssh_to "$h" "$env_prefix bash -s" <<< "$remote_script" &
done
wait

log "verify:"
log "  KUBECONFIG=$HECATON_ROOT/config/kubeconfig kubectl get nodes -o wide"
