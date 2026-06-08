#!/usr/bin/env bash
#
# Remove a single host from the hecaton fleet. Cleanly reverts everything
# our bootstrap scripts put on it, so the host is back to its original
# state. Idempotent: safe to re-run.
#
# Usage:  bash ops/remove-host.sh <inventory-name>
#
# Order matters:
#   1. cordon  — stop scheduling new pods to it
#   2. drain   — evict pods, ignoring DaemonSets
#   3. delete  — remove the Node object from the API
#   4. SSH in — run k3s-agent-uninstall.sh
#   5. SSH in — tailscale logout + apt purge
#
# NOTE: this does NOT edit config/hosts.yaml. Remove the entry by hand
# after the script reports success.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"

name="${1:-}"
[[ -n "$name" ]] || die "usage: bash ops/remove-host.sh <inventory-name>"

role="$(inventory_field "$name" role)"
[[ "$role" == "server" ]] && die "refusing to remove the k3s server '$name'; tear down the cluster instead"

ssh_host="$(inventory_field "$name" ssh_host)"
[[ -n "$ssh_host" ]] || die "host '$name' not found in inventory"

# 1-3. Drain + delete the Node object (if it exists).
export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
if [[ -f "$KUBECONFIG" ]] && command -v kubectl >/dev/null; then
  k8s_name="$(ssh_to "$name" 'hostname' | tr '[:upper:]' '[:lower:]')"
  if kubectl get node "$k8s_name" >/dev/null 2>&1; then
    log "cordoning $k8s_name"
    kubectl cordon "$k8s_name"
    log "draining $k8s_name"
    kubectl drain "$k8s_name" \
      --ignore-daemonsets \
      --delete-emptydir-data \
      --force \
      --grace-period=30 \
      --timeout=120s || warn "drain reported errors, continuing"
    log "deleting node object $k8s_name"
    kubectl delete node "$k8s_name"
  else
    log "no k8s node object for $k8s_name, skipping drain"
  fi
else
  warn "no kubeconfig or kubectl, skipping k8s cleanup"
fi

# 4-5. Tear down the host itself.
log "uninstalling k3s + tailscale on $name"
ssh_to "$name" 'bash -s' <<'REMOTE'
set -euo pipefail

# k3s agent: stock uninstaller is shipped by the installer.
if [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then
  echo "[remote] running k3s-agent-uninstall.sh"
  sudo /usr/local/bin/k3s-agent-uninstall.sh
elif [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
  echo "[remote] running k3s-uninstall.sh"
  sudo /usr/local/bin/k3s-uninstall.sh
else
  echo "[remote] no k3s uninstaller present, skipping"
fi

# Tailscale: leave the tailnet, then remove the package so the host is
# returned to its pre-hecaton state.
if command -v tailscale >/dev/null 2>&1; then
  echo "[remote] tailscale logout"
  sudo tailscale logout || true
  echo "[remote] purging tailscale package"
  sudo systemctl disable --now tailscaled 2>/dev/null || true
  sudo apt-get -y purge tailscale tailscale-archive-keyring 2>/dev/null || true
  sudo rm -f /etc/apt/sources.list.d/tailscale.list \
             /usr/share/keyrings/tailscale-archive-keyring.gpg
fi
REMOTE

log ""
log "$name removed from the fleet."
log "remaining steps (manual):"
log "  - remove the '$name' entry from config/hosts.yaml"
log "  - the tailnet still has a stale device record; remove it at"
log "    https://login.tailscale.com/admin/machines"
