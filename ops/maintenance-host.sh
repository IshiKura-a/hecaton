#!/usr/bin/env bash
#
# Put a node into maintenance mode or bring it back.
#
# Usage:
#   bash ops/maintenance-host.sh start <inventory-name> [--force]
#   bash ops/maintenance-host.sh stop  <inventory-name>
#
# "start" cordons the node (no new sandboxes scheduled) then either:
#   - (default) waits until all sandbox pods on that node finish and
#     are released by their trainers, or
#   - (--force) drains immediately — kills all sandbox pods on the
#     node. Trainers will see connection errors and must re-acquire.
#
# "stop" uncordons the node so scheduling resumes.
#
# What happens with --force:
#   All sandbox pods on the node are immediately evicted (kubectl drain).
#   Trainers holding those sandboxes will get connection errors on next
#   exec/heartbeat. The broker's reaper will clean up the stale entries
#   within 60s. Trainers must re-acquire to continue.
#
# Why default is wait: `kubectl drain` force-deletes pods, which kills
#   in-progress training tasks. For sandbox workloads the safe path
#   is cordon + wait-for-natural-release. Use --force only when you
#   need the node empty immediately (hardware failure, urgent patching).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"

action="${1:-}"
name="${2:-}"
flag="${3:-}"

[[ "$action" == "start" || "$action" == "stop" ]] || die "usage: bash ops/maintenance-host.sh {start|stop} <inventory-name> [--force]"
[[ -n "$name" ]] || die "usage: bash ops/maintenance-host.sh {start|stop} <inventory-name> [--force]"

ssh_host="$(inventory_field "$name" ssh_host)"
[[ -n "$ssh_host" ]] || die "host '$name' not found in inventory"

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG"

k8s_name="$(k8s_node_name_for "$name")"
kubectl get node "$k8s_name" >/dev/null 2>&1 || die "node '$k8s_name' not found in cluster"

NAMESPACE="${HECATON_NAMESPACE:-hecaton-sandboxes}"

case "$action" in
  start)
    log "cordoning $k8s_name (no new sandboxes will be scheduled here)"
    kubectl cordon "$k8s_name"

    if [[ "$flag" == "--force" ]]; then
      log "draining $k8s_name (force-killing all sandbox pods)..."
      kubectl drain "$k8s_name" \
        --ignore-daemonsets \
        --delete-emptydir-data \
        --force \
        --grace-period=30 \
        --timeout=120s || warn "drain reported errors, continuing"
      log "node $k8s_name drained. safe for maintenance."
      log "run 'bash ops/maintenance-host.sh stop $name' to uncordon when done."
      exit 0
    fi

    log "waiting for all sandbox pods on $k8s_name to drain naturally..."
    while true; do
      count=$(kubectl get pods -n "$NAMESPACE" --field-selector "spec.nodeName=$k8s_name" --no-headers 2>/dev/null | wc -l | tr -d ' ')
      if [[ "$count" -eq 0 ]]; then
        break
      fi
      log "  $count pod(s) remaining, waiting 30s..."
      sleep 30
    done
    log "node $k8s_name is idle. safe for maintenance."
    log "run 'bash ops/maintenance-host.sh stop $name' to uncordon when done."
    ;;

  stop)
    log "uncordoning $k8s_name"
    kubectl uncordon "$k8s_name"
    log "node $k8s_name is schedulable again."
    ;;
esac
