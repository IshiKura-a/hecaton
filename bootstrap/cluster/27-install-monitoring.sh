#!/usr/bin/env bash
#
# Phase 27: install monitoring stack. Thin wrapper around the Python
# entrypoint. Implementation lives in bootstrap/lib/monitoring.py.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG (run earlier bootstrap phases first)"
require_cmd kubectl
require_cmd helm

py="$(bootstrap_uv)"
exec "$py" "$HERE/27-install-monitoring.py" "$@"
