#!/usr/bin/env bash
#
# For every fleet host, detect its GPU vendor (AMD | NVIDIA | none),
# label the corresponding k3s node, and apply a device-plugin DaemonSet
# scoped to that single host. Per-host scoping lets us honor the
# optional `gpu_count` inventory field as a hard cap (sets
# HIP_VISIBLE_DEVICES / NVIDIA_VISIBLE_DEVICES on the plugin pod, so
# kubelet only ever sees `gpu_count` devices on that node).
#
# Idempotent: detection + labelling + `kubectl apply` all converge.
#
# Requires:
#   - config/hosts.yaml
#   - config/kubeconfig (produced by 20-install-k3s-server.sh)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"
source "$HECATON_ROOT/lib/gpu-version.sh"

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG (run earlier bootstrap phases first)"
require_cmd kubectl

# Detect GPU vendor on a host. Prints exactly one of: amd | nvidia | none.
detect_vendor() {
  local out
  out="$(ssh_to "$1" 'lspci -nn 2>/dev/null')"
  if   grep -q '\[1002:' <<<"$out"; then echo amd
  elif grep -q '\[10de:' <<<"$out"; then echo nvidia
  else                                   echo none
  fi
}

node_name_for() {
  ssh_to "$1" 'hostname' | tr '[:upper:]' '[:lower:]'
}

# Generate the device-string for a cap, e.g. count=4 -> "0,1,2,3".
device_list() {
  local n="$1" out=""
  for i in $(seq 0 $((n - 1))); do
    out+="${i},"
  done
  printf '%s' "${out%,}"
}

# Apply one per-host DaemonSet so that gpu_count caps apply individually.
apply_amd_daemonset() {
  local host="$1" node="$2" cap="$3"
  local visible env_name="HIP_VISIBLE_DEVICES" env_val
  if [[ -n "$cap" ]]; then
    env_val="$(device_list "$cap")"
  else
    env_val="all"
  fi
  visible="
        env:
        - name: $env_name
          value: \"$env_val\""

  kubectl apply -n kube-system -f - <<YAML
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: amdgpu-device-plugin-${host}
  labels:
    app.kubernetes.io/name: amdgpu-device-plugin
    app.kubernetes.io/managed-by: hecaton
    hecaton.io/host: ${host}
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: amdgpu-device-plugin
      hecaton.io/host: ${host}
  updateStrategy: { type: RollingUpdate }
  template:
    metadata:
      labels:
        app.kubernetes.io/name: amdgpu-device-plugin
        hecaton.io/host: ${host}
    spec:
      priorityClassName: system-node-critical
      nodeSelector:
        kubernetes.io/hostname: ${node}
      tolerations: [{ operator: Exists }]
      containers:
      - name: amdgpu-device-plugin
        image: ${AMD_DEVICE_PLUGIN_IMAGE}
        imagePullPolicy: IfNotPresent
        securityContext: { privileged: true }${visible}
        volumeMounts:
        - { name: device-plugin, mountPath: /var/lib/kubelet/device-plugins }
        - { name: sys,           mountPath: /sys }
        - { name: dev-kfd,       mountPath: /dev/kfd }
        - { name: dev-dri,       mountPath: /dev/dri }
      volumes:
      - { name: device-plugin, hostPath: { path: /var/lib/kubelet/device-plugins } }
      - { name: sys,           hostPath: { path: /sys } }
      - { name: dev-kfd,       hostPath: { path: /dev/kfd } }
      - { name: dev-dri,       hostPath: { path: /dev/dri } }
YAML
  kubectl -n kube-system rollout status "ds/amdgpu-device-plugin-${host}" --timeout=180s
}

apply_nvidia_daemonset() {
  local host="$1" node="$2" cap="$3"
  local visible env_val
  if [[ -n "$cap" ]]; then
    env_val="$(device_list "$cap")"
  else
    env_val="all"
  fi
  visible="
        env:
        - name: NVIDIA_VISIBLE_DEVICES
          value: \"$env_val\""

  kubectl apply -n kube-system -f - <<YAML
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-device-plugin-${host}
  labels:
    app.kubernetes.io/name: nvidia-device-plugin
    app.kubernetes.io/managed-by: hecaton
    hecaton.io/host: ${host}
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: nvidia-device-plugin
      hecaton.io/host: ${host}
  updateStrategy: { type: RollingUpdate }
  template:
    metadata:
      labels:
        app.kubernetes.io/name: nvidia-device-plugin
        hecaton.io/host: ${host}
    spec:
      priorityClassName: system-node-critical
      runtimeClassName: nvidia
      nodeSelector:
        kubernetes.io/hostname: ${node}
      tolerations: [{ operator: Exists }]
      containers:
      - name: nvidia-device-plugin
        image: ${NVIDIA_DEVICE_PLUGIN_IMAGE}
        imagePullPolicy: IfNotPresent
        securityContext:
          allowPrivilegeEscalation: false
          capabilities: { drop: ["ALL"] }${visible}
        volumeMounts:
        - { name: device-plugin, mountPath: /var/lib/kubelet/device-plugins }
      volumes:
      - { name: device-plugin, hostPath: { path: /var/lib/kubelet/device-plugins } }
YAML
  kubectl -n kube-system rollout status "ds/nvidia-device-plugin-${host}" --timeout=180s
}

# --- per-host loop ---------------------------------------------------------

# Two parallel arrays so this works under bash 3.2 (macOS).
nodes=()
vendors=()
for h in $(inventory_hosts); do
  vendor="$(detect_vendor "$h")"
  node="$(node_name_for "$h")"
  cap="$(inventory_field "$h" gpu_count)"
  nodes+=("$node")
  vendors+=("$vendor")

  log "==> $h ($node): vendor=$vendor cap=${cap:-<all>}"
  kubectl label node "$node" "hecaton.io/gpu-vendor=$vendor" --overwrite >/dev/null

  case "$vendor" in
    amd)    apply_amd_daemonset    "$h" "$node" "$cap" ;;
    nvidia) apply_nvidia_daemonset "$h" "$node" "$cap" ;;
    none)   : ;;
  esac
done

# --- report -----------------------------------------------------------------

log ""
log "advertised GPU capacity:"
for i in "${!nodes[@]}"; do
  node="${nodes[$i]}"
  vendor="${vendors[$i]}"
  case "$vendor" in
    amd)    res="amd.com/gpu"    ;;
    nvidia) res="nvidia.com/gpu" ;;
    *)      printf '  %-22s %s\n' "$node" "(none)"; continue ;;
  esac
  count="$(kubectl get node "$node" -o jsonpath="{.status.capacity.${res//./\\.}}" 2>/dev/null)"
  printf '  %-22s %s = %s\n' "$node" "$res" "${count:-0}"
done
