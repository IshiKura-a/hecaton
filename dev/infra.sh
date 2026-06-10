#!/usr/bin/env bash
#
# Infra iteration. Same shape as dev/iter.sh, but for cluster-side
# infrastructure (monitoring, GPU device plugins, agent-sandbox
# controller, sandbox catalog) instead of broker/scaffolds.
#
# Each phase is hash-gated: if nothing under its watched paths
# changed since the last successful run, it's skipped. This lets you
# iterate on dashboards or sandbox declarations without re-running
# slow phases.
#
# Usage:
#   dev/infra.sh                   re-run every changed phase
#
# Env knobs:
#   ONLY=<phase>                   single phase. Names:
#                                    monitoring-core
#                                    monitoring-dashboards
#                                    monitoring-exporters
#                                    device-plugins
#                                    agent-sandbox
#                                    sandboxes
#   SKIP_<NAME>=1                  skip a phase
#   FORCE_<NAME>=1                 re-run even when content hash matches
#
# State cache:
#   .cache/dev-infra/<phase>.hash  last-deployed content hash. Wipe
#                                  the dir to force-redo every phase.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/version.sh"

STATE_DIR="$HECATON_ROOT/.cache/dev-infra"
mkdir -p "$STATE_DIR"

ONLY="${ONLY:-}"

want() {
  local name="$1"
  if [[ -n "$ONLY" && "$ONLY" != "$name" ]]; then return 1; fi
  local skip_var
  skip_var="SKIP_$(echo "$name" | tr '[:lower:]-' '[:upper:]_')"
  if [[ -n "${!skip_var:-}" ]]; then
    log "$name: skipped (\$$skip_var set)"
    return 1
  fi
  return 0
}

# run_phase <name> <phase-script> <extra-watched-path>...
#
# Hash-gate around a phase script. The script itself is always part of
# the watched set, plus any extra paths it consumes. The script must
# already be idempotent (every bootstrap phase is).
#
# Per-call script args can be passed via $STEP_ARG (single token), set
# inline so the scope is one run_phase call:
#
#   STEP_ARG=core run_phase monitoring-core 27-install-monitoring.sh ...
run_phase() {
  local name="$1"; shift
  local script="$1"; shift
  local watched=("$script" "$@")
  local step_arg="${STEP_ARG:-}"

  want "$name" || return 0

  local cur stamp
  cur="$(hash_paths "${watched[@]}")"
  stamp="$STATE_DIR/$name.hash"

  local force_var
  force_var="FORCE_$(echo "$name" | tr '[:lower:]-' '[:upper:]_')"
  if [[ -z "${!force_var:-}" && -f "$stamp" && "$(cat "$stamp")" == "$cur" ]]; then
    log "$name: unchanged (hash ${cur:0:12}), skip"
    return 0
  fi
  log "==== $name (hash ${cur:0:12}) ===="
  if [[ -n "$step_arg" ]]; then
    bash "$HECATON_ROOT/$script" "$step_arg"
  else
    bash "$HECATON_ROOT/$script"
  fi
  echo "$cur" > "$stamp"
}

# --- phases ---------------------------------------------------------------
#
# Watched paths are kept narrow on purpose: a phase only re-runs when
# files it actually consumes change.
#
# Monitoring is split into three sub-steps because helm upgrade
# (monitoring-core) is ~80s while the other two are seconds — gating
# them together would make every dashboard tweak pay the helm cost.

_MON_COMMON=(
  bootstrap/cluster/27-install-monitoring.py
  bootstrap/lib
)

STEP_ARG=core run_phase monitoring-core \
  bootstrap/cluster/27-install-monitoring.sh \
  "${_MON_COMMON[@]}" \
  platform/monitoring/values.yaml \
  lib/monitoring-version.sh

STEP_ARG=dashboards run_phase monitoring-dashboards \
  bootstrap/cluster/27-install-monitoring.sh \
  "${_MON_COMMON[@]}" \
  platform/monitoring/dashboards

STEP_ARG=exporters run_phase monitoring-exporters \
  bootstrap/cluster/27-install-monitoring.sh \
  "${_MON_COMMON[@]}" \
  lib/monitoring-version.sh \
  config/hosts.yaml

run_phase device-plugins \
  bootstrap/cluster/22-install-device-plugins.sh \
  bootstrap/cluster/22-install-device-plugins.py \
  bootstrap/lib \
  lib/gpu-version.sh

run_phase agent-sandbox \
  bootstrap/cluster/23-install-agent-sandbox.sh \
  lib/agent-sandbox-version.sh

run_phase sandboxes \
  bootstrap/cluster/24-apply-sandboxes.sh \
  bootstrap/cluster/24-apply-sandboxes.py \
  bootstrap/lib \
  config/sandboxes
