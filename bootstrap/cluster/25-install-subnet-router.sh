#!/usr/bin/env bash
#
# Deploy a single Tailscale subnet router pod inside the cluster that
# advertises the pod CIDR (10.42.0.0/16, k3s default) into the tailnet.
# The tailnet policy's `autoApprovers.routes` accepts routes coming from
# `tag:fleet-subnet`, so trainers in the tailnet can reach sandbox pods
# at their pod IPs without manual approval.
#
# Requires: TS_AUTHKEY_SUBNET_ROUTER in .env (Tailscale auth key for a
# device that will carry tag:fleet-subnet).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
load_env
require_var TS_AUTHKEY_SUBNET_ROUTER

export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG"
require_cmd kubectl

ns=hecaton-system
kubectl get namespace "$ns" >/dev/null 2>&1 \
  || kubectl create namespace "$ns" >/dev/null

# Auth key as Secret. Re-applied on every run; rotation = update .env
# and re-run this script.
kubectl create secret generic tailscale-subnet-router-auth \
  -n "$ns" \
  --from-literal=TS_AUTHKEY="$TS_AUTHKEY_SUBNET_ROUTER" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -n "$ns" -f "$HECATON_ROOT/platform/network/subnet-router.yaml"
kubectl -n "$ns" rollout status deploy/tailscale-subnet-router --timeout=120s
