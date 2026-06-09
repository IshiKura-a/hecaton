#!/usr/bin/env bash
#
# Build the broker image directly on the k3s control-plane host (which
# already has docker + outbound internet), then import it into the
# containerd of every fleet host. Avoids any laptop-side container
# tooling.
#
# Usage: bash platform/broker/build-and-import.sh
#
# Builds with the tag from $BROKER_IMAGE in .env, or
# `docker.io/library/hecaton-broker:dev` by default.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/inventory.sh"
source "$HECATON_ROOT/lib/remote.sh"
load_env

image="${BROKER_IMAGE:-docker.io/library/hecaton-broker:dev}"

# Only the conventional local tag (docker.io/library/...) triggers a
# local build + scp + import. Anything else looks like a real registry
# image (ghcr.io/..., *.azurecr.io/..., a user namespace under
# docker.io, etc.) — let containerd pull it on demand when the broker
# Deployment lands. This keeps `bootstrap/install.sh` cheap in prod
# mode while still being correct in offline/dev mode.
case "$image" in
  docker.io/library/*) : ;;
  *)
    log "BROKER_IMAGE=$image looks like a registry image; fleet nodes will pull it on demand."
    log "skipping local build+import. (Force local mode with BROKER_IMAGE=docker.io/library/hecaton-broker:dev.)"
    exit 0
    ;;
esac

# Pick the server host as the build host: it's always present in the
# inventory and we already require docker-class tooling there for k3s.
build_host=""
for h in $(inventory_hosts); do
  if [[ "$(inventory_field "$h" role)" == "server" ]]; then
    build_host="$h"
    break
  fi
done
[[ -n "$build_host" ]] || die "no role:server host in inventory"

log "build host: $build_host"
log "target image: $image"

# 1) copy the broker source over
remote_src=/tmp/hecaton-broker-src
log "==> $build_host: uploading sources"
ssh_to "$build_host" "rm -rf $remote_src && mkdir -p $remote_src"
scp_to "$build_host" "$HECATON_ROOT/platform/broker/Dockerfile"   "$remote_src/Dockerfile"
scp_to "$build_host" "$HECATON_ROOT/platform/broker/pyproject.toml" "$remote_src/pyproject.toml"
scp_to "$build_host" "$HECATON_ROOT/platform/broker/broker.py"    "$remote_src/broker.py"

# 2) build + export to a tarball on the build host
log "==> $build_host: docker build"
ssh_to "$build_host" "
  set -euo pipefail
  cd $remote_src
  sudo docker build -t $image .
  sudo docker save $image -o /tmp/hecaton-broker.tar
  sudo chmod 644 /tmp/hecaton-broker.tar
"

# 3) import on the build host's containerd
log "==> $build_host: importing into k3s containerd"
ssh_to "$build_host" 'sudo k3s ctr images import /tmp/hecaton-broker.tar'

# 4) ship the tarball to every other host. We use the laptop as a
#    transit point because fleet hosts don't have direct ssh access to
#    each other (the tailnet ACL only grants ssh from group:fleet-ops).
others=()
for h in $(inventory_hosts); do
  [[ "$h" == "$build_host" ]] && continue
  others+=("$h")
done

if (( ${#others[@]} > 0 )); then
  local_tar="$(mktemp -t hecaton-broker.XXXX.tar)"
  trap 'rm -f "$local_tar"' EXIT
  log "pulling tarball from $build_host to laptop"
  ssh_to "$build_host" 'sudo cat /tmp/hecaton-broker.tar' > "$local_tar"

  for h in "${others[@]}"; do
    log "==> $h: copying tarball"
    scp_to "$h" "$local_tar" /tmp/hecaton-broker.tar
    log "==> $h: importing into k3s containerd"
    # `sudo rm` because the tarball may end up root-owned after import,
    # and /tmp's sticky bit forbids non-owner deletion.
    ssh_to "$h" 'sudo k3s ctr images import /tmp/hecaton-broker.tar && sudo rm -f /tmp/hecaton-broker.tar'
  done
fi

# 5) clean up build artifact on the build host
ssh_to "$build_host" 'sudo rm -f /tmp/hecaton-broker.tar && rm -rf '"$remote_src"

log ""
log "image present on all fleet nodes as: $image"
log "set BROKER_IMAGE=$image in .env if it isn't already, then:"
log "  bash bootstrap/cluster/26-install-broker.sh"
