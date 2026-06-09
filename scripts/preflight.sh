#!/usr/bin/env bash
#
# Local preflight: make sure the laptop has the tools hecaton needs.
# Single source of truth for laptop-side dependencies.
#
# On macOS: auto-installs missing tools via Homebrew, no prompts.
# On Linux: auto-installs via apt-get if available; otherwise prints
#   per-tool install hints and exits non-zero.
# On other OSes: prints install hints and exits non-zero.
#
# Written for bash 3.2 (macOS default) — no associative arrays.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
source "$HECATON_ROOT/lib/common.sh"

# OS deps that must already be on the box — we don't install OpenSSH.
for cmd in ssh scp awk curl rsync; do
  command -v "$cmd" >/dev/null \
    || die "missing $cmd (expected to come with the OS)"
done

# Per-tool descriptors:
#   "cmd|brew-pkg|brew-mode|apt-pkg|upstream-hint"
#   brew-mode is "formula" or "cask".
#   Use "-" in brew-pkg or apt-pkg when that manager doesn't ship the
#   tool — the user gets the upstream-hint instead.
TOOLS=(
  "tailscale|tailscale|cask|-|https://tailscale.com/download"
  "kubectl|kubectl|formula|kubectl|https://kubernetes.io/docs/tasks/tools/#kubectl"
  "gh|gh|formula|gh|https://cli.github.com/"
)

os="$(uname -s)"
apt_available=0
if [[ "$os" == "Linux" ]] && command -v apt-get >/dev/null 2>&1; then
  apt_available=1
fi

missing_specs=()
for spec in "${TOOLS[@]}"; do
  cmd="${spec%%|*}"
  command -v "$cmd" >/dev/null 2>&1 && continue
  missing_specs+=("$spec")
done

if (( ${#missing_specs[@]} == 0 )); then
  log "preflight ok."
  exit 0
fi

if [[ "$os" == "Darwin" ]]; then
  command -v brew >/dev/null \
    || die "missing tools but Homebrew not installed; get it at https://brew.sh first"
  for spec in "${missing_specs[@]}"; do
    IFS='|' read -r cmd brew_pkg brew_mode apt_pkg hint <<< "$spec"
    if [[ "$brew_pkg" == "-" ]]; then
      die "missing $cmd — install manually: $hint"
    fi
    log "installing $cmd via brew"
    if [[ "$brew_mode" == "cask" ]]; then
      brew install --cask "$brew_pkg"
    else
      brew install "$brew_pkg"
    fi
  done
  log "preflight ok."
  exit 0
fi

if (( apt_available )); then
  apt_pkgs=()
  hint_lines=()
  for spec in "${missing_specs[@]}"; do
    IFS='|' read -r cmd brew_pkg brew_mode apt_pkg hint <<< "$spec"
    case "$cmd" in
      # tailscale: official one-liner sets up the apt repo and installs.
      tailscale)
        log "installing tailscale via upstream script (sudo)"
        curl -fsSL https://tailscale.com/install.sh | sh
        ;;
      *)
        if [[ "$apt_pkg" == "-" ]]; then
          hint_lines+=("$cmd  ->  $hint")
        else
          apt_pkgs+=("$apt_pkg")
        fi
        ;;
    esac
  done
  if (( ${#apt_pkgs[@]} > 0 )); then
    log "installing via apt: ${apt_pkgs[*]} (sudo)"
    sudo apt-get update
    sudo apt-get install -y "${apt_pkgs[@]}"
  fi
  if (( ${#hint_lines[@]} > 0 )); then
    warn "the following must be installed manually:"
    for line in "${hint_lines[@]}"; do warn "  $line"; done
    exit 1
  fi
  log "preflight ok."
  exit 0
fi

warn "auto-install not supported on $os; install these manually:"
for spec in "${missing_specs[@]}"; do
  IFS='|' read -r cmd brew_pkg brew_mode apt_pkg hint <<< "$spec"
  warn "  $cmd  ->  $hint"
done
exit 1
