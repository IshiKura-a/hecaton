#!/usr/bin/env bash
#
# Install the upstream agent-sandbox controllers + CRDs at the version
# pinned in lib/agent-sandbox-version.sh. Pulls the official release
# bundles and `kubectl apply`s them.
#
#   manifest.yaml   core controller + Sandbox CRD
#   extensions.yaml extensions controller + SandboxClaim/Template/WarmPool CRDs
#
# We use the released bundles (not raw k8s/*.yaml from main) because the
# unreleased manifests still contain `ko://...` image placeholders that
# only ko's build process resolves.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/agent-sandbox-version.sh"

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG"
require_cmd kubectl

base="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}"

log "applying agent-sandbox core (${AGENT_SANDBOX_VERSION})"
kubectl apply -f "${base}/manifest.yaml"

log "applying agent-sandbox extensions (${AGENT_SANDBOX_VERSION})"
kubectl apply -f "${base}/extensions.yaml"

log "waiting for controllers to become ready"
kubectl -n agent-sandbox-system rollout status deploy --timeout=180s
