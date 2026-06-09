#!/usr/bin/env bash
#
# One-key bootstrap: run every phase in order on the inventory.
# Each phase script is idempotent, so re-running is safe.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"

# Pass --force to re-apply k3s config even if version matches.
if [[ "${1:-}" == "--force" ]]; then
  export HECATON_FORCE=1
fi

# Fail fast if the laptop is missing tools we depend on later.
bash "$HECATON_ROOT/scripts/preflight.sh"

phases=(
  "bootstrap/network/10-install-tailscale.sh"
  "bootstrap/cluster/20-install-k3s-server.sh"
  "bootstrap/cluster/21-install-k3s-agent.sh"
  "bootstrap/cluster/22-install-device-plugins.sh"
  "bootstrap/cluster/23-install-agent-sandbox.sh"
  "bootstrap/cluster/24-apply-templates.sh"
  "bootstrap/cluster/25-install-subnet-router.sh"
  "bootstrap/cluster/27-stage-agent-tools.sh"
  # Broker image must be on the fleet nodes before phase 26 applies the
  # Deployment (imagePullPolicy: IfNotPresent). build-and-import.sh is
  # idempotent — it re-builds and re-imports, which is also how broker
  # code changes propagate.
  "platform/broker/build-and-import.sh"
  "bootstrap/cluster/26-install-broker.sh"
)

for phase in "${phases[@]}"; do
  log "==== $phase ===="
  bash "$HECATON_ROOT/$phase"
done

log ""
log "bootstrap complete."
log "  KUBECONFIG=$HECATON_ROOT/config/kubeconfig kubectl get nodes -o wide"
