#!/usr/bin/env bash
#
# Apply every SandboxTemplate in config/templates/ to the cluster.
# Idempotent: `kubectl apply` converges. New templates land by dropping
# a yaml in config/templates/ and re-running this script (or the full
# bootstrap/install.sh).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG (run earlier bootstrap phases first)"
require_cmd kubectl

ns=hecaton-sandboxes
kubectl get namespace "$ns" >/dev/null 2>&1 \
  || kubectl create namespace "$ns" >/dev/null

dir="$HECATON_ROOT/config/templates"
if [[ ! -d "$dir" ]] || [[ -z "$(ls -A "$dir" 2>/dev/null)" ]]; then
  log "no templates in $dir, skipping"
  exit 0
fi

log "applying SandboxTemplates from $dir"
kubectl apply -n "$ns" -f "$dir"
