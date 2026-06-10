#!/usr/bin/env bash
#
# Render every sandbox declaration under config/sandboxes/ into
# SandboxTemplate CRs and `kubectl apply` them. Handles two file kinds:
#
#   kind: Sandbox        — one SandboxTemplate, fully hand-authored.
#   kind: SandboxSource  — pull a list of images from a remote
#                          dataset (e.g. HuggingFace) and render one
#                          SandboxTemplate per row.
#
# Anything previously applied with label hecaton.io/managed-by=hecaton
# that's not in the new set gets deleted, so removing an entry from
# the dataset / yaml propagates to the cluster.
#
# Idempotent: re-running converges; only changed templates roll.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG (run earlier bootstrap phases first)"
require_cmd kubectl

dir="$HECATON_ROOT/config/sandboxes"
if [[ ! -d "$dir" ]] || [[ -z "$(ls -A "$dir" 2>/dev/null)" ]]; then
  log "no sandbox declarations in $dir, skipping"
  log "  drop yaml files (kind: Sandbox or SandboxSource) under that dir to enable"
  exit 0
fi

# pyyaml + huggingface_hub come from bootstrap/pyproject.toml.
py="$(bootstrap_uv)"

log "rendering sandbox declarations from $dir"
"$py" "$HERE/24-apply-sandboxes.py" "$dir"
