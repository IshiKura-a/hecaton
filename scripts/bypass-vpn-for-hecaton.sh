#!/usr/bin/env bash
# Route hecaton fleet tailnet IPs directly through the local Tailscale utun
# interface on macOS, bypassing broad VPN routes that may capture 100.64.0.0/10.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
export HECATON_ROOT

# shellcheck source=../lib/common.sh
source "$HECATON_ROOT/lib/common.sh"
# shellcheck source=../lib/inventory.sh
source "$HECATON_ROOT/lib/inventory.sh"
# shellcheck source=../lib/remote.sh
source "$HECATON_ROOT/lib/remote.sh"

apply=0
watch_interval=""

usage() {
  cat <<EOF
usage: $0 [--apply] [--watch SECONDS]

Fetch every host's Tailscale IPv4 from config/hosts.yaml and make host routes
point at the local Tailscale interface instead of a VPN route.

Default is dry-run. Use --apply to run sudo route delete/add.
--watch repeats forever, useful after VPN/Tailscale reconnects.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      apply=1
      shift
      ;;
    --watch)
      [[ $# -ge 2 ]] || die "--watch requires seconds"
      watch_interval="$2"
      [[ "$watch_interval" =~ ^[0-9]+$ ]] || die "--watch must be an integer number of seconds"
      (( watch_interval > 0 )) || die "--watch must be greater than 0"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || die "this script currently supports macOS only"
require_cmd tailscale
require_cmd ifconfig
require_cmd route

tailscale_iface() {
  local self_ip iface
  self_ip="$(tailscale ip -4 | head -1)"
  [[ -n "$self_ip" ]] || die "could not read local Tailscale IPv4"

  for iface in $(ifconfig -l); do
    if ifconfig "$iface" | grep -q "inet $self_ip "; then
      printf '%s\n' "$iface"
      return 0
    fi
  done
  die "could not find interface for local Tailscale IPv4 $self_ip"
}

host_tailnet_ip() {
  local host="$1" ip
  ip="$(ssh_to "$host" 'tailscale ip -4 | head -1' | tr -d '[:space:]')"
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "$host: invalid tailscale ip: ${ip:-<empty>}"
  printf '%s\n' "$ip"
}

current_route_iface() {
  local ip="$1"
  route -n get "$ip" 2>/dev/null | awk '/interface:/{print $2; exit}' || true
}

rewrite_route() {
  local ip="$1" iface="$2" current
  current="$(current_route_iface "$ip")"
  if [[ "$current" == "$iface" ]]; then
    log "$ip already routes via $iface"
    return 0
  fi

  if [[ "$apply" == 1 ]]; then
    log "$ip: ${current:-unknown} -> $iface"
    sudo route -n delete -host "$ip" >/dev/null 2>&1 || true
    sudo route -n add -host "$ip" -interface "$iface" >/dev/null
  else
    printf 'dry-run: %s current=%s target=%s\n' "$ip" "${current:-unknown}" "$iface"
    printf '  sudo route -n delete -host %q 2>/dev/null || true\n' "$ip"
    printf '  sudo route -n add -host %q -interface %q\n' "$ip" "$iface"
  fi
}

run_once() {
  local iface host ip
  iface="$(tailscale_iface)"
  log "local Tailscale interface: $iface"

  for host in $(inventory_hosts); do
    ip="$(host_tailnet_ip "$host")"
    log "$host tailnet ip: $ip"
    rewrite_route "$ip" "$iface"
  done
}

if [[ -n "$watch_interval" ]]; then
  while true; do
    run_once
    sleep "$watch_interval"
  done
else
  run_once
fi
