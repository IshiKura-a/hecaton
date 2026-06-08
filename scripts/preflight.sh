#!/usr/bin/env bash
#
# Local preflight: make sure the laptop has the tools hecaton needs.
# On macOS, auto-installs missing pieces via Homebrew. No prompts.
# On other OSes, prints what's missing and exits — install manually.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/.." && pwd)"
source "$HECATON_ROOT/lib/common.sh"

# 1. OS deps — must already be on the box. We do not install OpenSSH etc.
for cmd in ssh scp awk curl; do
  command -v "$cmd" >/dev/null \
    || die "missing $cmd (expected to come with the OS)"
done

# 2. Auto-installable deps. On macOS we use brew; elsewhere we tell the
# user to install manually.
brew_install() {
  local cmd="$1" pkg="$2" mode="${3:-formula}"
  command -v "$cmd" >/dev/null && return 0
  if [[ "$(uname -s)" != Darwin ]]; then
    die "missing $cmd; auto-install only supported on macOS — install manually"
  fi
  command -v brew >/dev/null \
    || die "missing $cmd and Homebrew is not installed; install from https://brew.sh first"
  log "installing $cmd via brew"
  if [[ "$mode" == cask ]]; then
    brew install --cask "$pkg"
  else
    brew install "$pkg"
  fi
}

brew_install tailscale tailscale cask
brew_install kubectl   kubectl

log "preflight ok."
