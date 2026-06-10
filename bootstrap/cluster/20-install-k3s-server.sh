#!/usr/bin/env bash
#
# Install or verify the k3s server on the host with `role: server` in
# config/hosts.yaml. Pins the version from lib/k3s-version.sh. Idempotent:
# if the host already runs the pinned version, this is a no-op.
#
# On success, writes two files to config/ (both gitignored):
#   - kubeconfig          kubectl access, server URL rewritten to tailnet IP
#   - k3s-node-token      join token for k3s agents

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"
source "$HECATON_ROOT/lib/k3s-version.sh"

# Locate the single role:server host.
server=""
for h in $(inventory_hosts); do
  if [[ "$(inventory_field "$h" role)" == "server" ]]; then
    [[ -z "$server" ]] || die "more than one host has role: server ('$server' and '$h')"
    server="$h"
  fi
done
[[ -n "$server" ]] || die "no host with role: server in $(inventory_path)"

force="${HECATON_FORCE:-}"

log "k3s $K3S_VERSION server target: $server"

# Remote installer. Idempotent: if the running k3s already matches the
# pinned version, do nothing. Otherwise install/upgrade.
remote_script=$(cat <<'REMOTE'
set -euo pipefail
WANT="$K3S_VERSION_REMOTE"

ts_ip="$(tailscale ip -4 | head -1)"
[[ -n "$ts_ip" ]] || { echo "no tailscale ipv4 on this host" >&2; exit 1; }

current=""
if command -v k3s >/dev/null 2>&1; then
  current="$(k3s --version 2>/dev/null | head -1 | awk '{print $3}')"
fi

if [[ "$current" == "$WANT" ]] && [[ -z "${FORCE_REMOTE:-}" ]]; then
  echo "[remote] k3s $WANT already installed on $(hostname) (use --force to reconfigure)"
else
  if [[ -n "$current" ]]; then
    echo "[remote] upgrading k3s $current -> $WANT"
  else
    echo "[remote] installing k3s $WANT"
  fi
  # `INSTALL_K3S_EXEC` becomes the systemd ExecStart args for k3s.
  #   --flannel-iface=tailscale0  pod CNI rides the tailnet
  #   --node-ip                   advertise tailnet IP as the node's internal IP
  #   --advertise-address         apiserver advertises the same address
  #   --disable=traefik,servicelb we provide ingress / LB ourselves later
  #   --write-kubeconfig-mode=644 readable by non-root for `scp`/`cat`
  # Default max-pods to CPU count if not configured.
  max_pods="${MAX_PODS_REMOTE:-$(nproc)}"

  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_VERSION="$WANT" \
    INSTALL_K3S_EXEC="server \
      --flannel-iface=tailscale0 \
      --node-ip=$ts_ip \
      --advertise-address=$ts_ip \
      --disable=traefik \
      --disable=servicelb \
      --write-kubeconfig-mode=644 \
      --kube-scheduler-arg=config=/etc/rancher/k3s/scheduler-config.yaml \
      --kubelet-arg=max-pods=$max_pods \
      --kubelet-arg=image-gc-high-threshold=85 \
      --kubelet-arg=image-gc-low-threshold=70 \
      --kubelet-arg=serialize-image-pulls=false \
      --kubelet-arg=max-parallel-image-pulls=16 \
      --kubelet-arg=eviction-hard= \
      --kubelet-arg=eviction-soft=" \
    sh -
fi

# Wait for apiserver to be ready. ~60s budget.
for _ in $(seq 1 30); do
  if sudo k3s kubectl get --raw=/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
sudo k3s kubectl get --raw=/healthz >/dev/null 2>&1 \
  || { echo "k3s apiserver did not become healthy" >&2; exit 1; }
REMOTE
)

# Upload scheduler config before install.
log "uploading scheduler-config.yaml to $server"
scp_to "$server" "$HECATON_ROOT/config/scheduler-config.yaml" "/tmp/scheduler-config.yaml"
ssh_to "$server" "sudo mkdir -p /etc/rancher/k3s && sudo mv /tmp/scheduler-config.yaml /etc/rancher/k3s/scheduler-config.yaml"

max_pods="$(inventory_field "$server" max_pods 2>/dev/null || true)"
ssh_to "$server" "K3S_VERSION_REMOTE=$(printf '%q' "$K3S_VERSION") MAX_PODS_REMOTE=$(printf '%q' "$max_pods") FORCE_REMOTE=$(printf '%q' "$force") bash -s" <<< "$remote_script"

# Pull what we need from the server, one ssh per artifact (keeps parsing trivial).
server_ip="$(ssh_to "$server" 'tailscale ip -4 | head -1')"
[[ -n "$server_ip" ]] || die "could not read server tailnet IP"

mkdir -p "$HECATON_ROOT/config"
umask 077

ssh_to "$server" 'sudo cat /var/lib/rancher/k3s/server/node-token' \
  > "$HECATON_ROOT/config/k3s-node-token"
[[ -s "$HECATON_ROOT/config/k3s-node-token" ]] || die "node-token empty"

ssh_to "$server" 'sudo cat /etc/rancher/k3s/k3s.yaml' \
  | sed "s|server: https://127.0.0.1:6443|server: https://$server_ip:6443|" \
  > "$HECATON_ROOT/config/kubeconfig"
[[ -s "$HECATON_ROOT/config/kubeconfig" ]] || die "kubeconfig empty"

log "wrote config/kubeconfig (server: https://$server_ip:6443)"
log "wrote config/k3s-node-token"
log ""
log "verify:"
log "  KUBECONFIG=$HECATON_ROOT/config/kubeconfig kubectl get nodes -o wide"
log "next: bash bootstrap/cluster/21-install-k3s-agent.sh"
