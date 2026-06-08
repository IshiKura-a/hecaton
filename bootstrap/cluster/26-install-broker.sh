#!/usr/bin/env bash
#
# Deploy the hecaton broker (Deployment + ClusterRole/Binding + Secret +
# NodePort Service) into the cluster.
#
# Requires:
#   .env with BROKER_IMAGE (already built & pushed) and HECATON_TOKEN
#   config/kubeconfig

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
load_env
require_var BROKER_IMAGE
require_var HECATON_TOKEN

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG"
require_cmd kubectl

# Render the manifest with image + token substituted, then apply.
# Using sed (not envsubst) so we have no extra dependency.
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed -e "s|__BROKER_IMAGE__|${BROKER_IMAGE}|g" \
    -e "s|__HECATON_TOKEN__|${HECATON_TOKEN}|g" \
    "$HECATON_ROOT/platform/broker/deployment.yaml" > "$rendered"

kubectl apply -f "$rendered"
kubectl -n hecaton-system rollout status deploy/hecaton-broker --timeout=120s

log ""
log "broker NodePort: http://<any-fleet-host-tailnet-ip>:30443"
log "set HECATON_BROKER_URL on trainers accordingly."
