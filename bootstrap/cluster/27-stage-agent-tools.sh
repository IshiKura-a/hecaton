#!/usr/bin/env bash
#
# Stage agent scaffold tools (R2E-Gym, others) onto every fleet host so
# the broker can mount them into sandbox pods via hostPath at acquire
# time. Files land at /opt/hecaton/agent-tools/<scaffold>/, mode 0555
# (r-x, no write) — the broker injects a readOnly mount, so pods get
# read + execute only with zero in-pod copy.
#
# Source of truth is scaffolds/<scaffold>/ on the laptop.
# SandboxTemplate YAML stays clean — scaffold is a trainer-side concept
# layered in by the broker, not baked into the cluster config.
#
# Always re-stages every scaffold (idempotent on identical content;
# overwrites on content change). Refuses to run if any Sandbox CR
# exists in hecaton-sandboxes, because hostPath is a bind mount:
# replacing files under a live sandbox would silently swap its tools
# mid-rollout. Release sandboxes first (the trainer's
# provider.revoke() or 'kubectl delete sandbox -n hecaton-sandboxes
# --all'), then re-run.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HECATON_ROOT="$(cd "$HERE/../.." && pwd)"
export HECATON_ROOT

source "$HECATON_ROOT/lib/common.sh"
source "$HECATON_ROOT/lib/remote.sh"

require_cmd tar

src="$HECATON_ROOT/scaffolds"
remote_dir="/opt/hecaton/agent-tools"

if [[ ! -d "$src" ]] || [[ -z "$(ls -A "$src" 2>/dev/null)" ]]; then
  log "no scaffolds in $src, skipping"
  log "  drop tool sets under scaffolds/<scaffold>/ to enable this"
  exit 0
fi

# Enumerate scaffolds (immediate subdirs only, skip dotfiles).
# Shell glob instead of `find -printf` so we work with BSD find too.
scaffolds=()
for d in "$src"/*/; do
  [[ -d "$d" ]] || continue
  name="${d%/}"; name="${name##*/}"
  [[ "$name" == .* ]] && continue
  scaffolds+=("$name")
done
[[ ${#scaffolds[@]} -gt 0 ]] || die "$src has files but no scaffold subdirs"

# Safety: re-staging while a sandbox is alive would swap its mounted
# tools mid-rollout. The cluster is the single source of truth here
# — broker's in-memory accounting is derived state and not consulted.
export KUBECONFIG="$HECATON_ROOT/config/kubeconfig"
[[ -f "$KUBECONFIG" ]] || die "missing $KUBECONFIG (run earlier bootstrap phases first)"
require_cmd kubectl

ns=hecaton-sandboxes
alive="$(kubectl get sandbox -n "$ns" --ignore-not-found -o name 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$alive" != "0" ]]; then
  warn "$alive Sandbox CR(s) still exist in $ns:"
  kubectl get sandbox -n "$ns" -o wide >&2 || true
  die "re-staging scaffold tools would swap mounts under live sandboxes; release them first (provider.revoke() or 'kubectl delete sandbox -n $ns --all'), then re-run."
fi

# `while read` instead of `mapfile` so bash 3.2 (macOS default) works.
hosts=()
while IFS= read -r h; do
  hosts+=("$h")
done < <(inventory_hosts)
[[ ${#hosts[@]} -gt 0 ]] || die "inventory has no hosts"

log "scaffolds: ${scaffolds[*]}"
log "hosts:     ${hosts[*]}"

for h in "${hosts[@]}"; do
  log "==> $h"
  # Wipe + recreate so removed files don't linger across stages.
  ssh_to "$h" "sudo rm -rf $remote_dir && sudo mkdir -p $remote_dir"

  for s in "${scaffolds[@]}"; do
    log "    stage $s"
    # Build the tar stream from a transient copy of the scaffold so we
    # can apply local patches before the bytes hit the wire — the
    # fleet host then sees a clean tree with no _patches/ directory
    # and no patch tool dependency. See scaffolds/<s>/_patches/ for
    # the list of patches and why they exist.
    work="$(mktemp -d -t hecaton-stage.XXXX)"
    # shellcheck disable=SC2064
    trap "rm -rf '$work'" EXIT
    # cp -R then exclude _patches/ in tar; simpler than glob exclusions
    # that vary between BSD/GNU tar.
    cp -R "$src/$s/." "$work/"
    if [[ -d "$src/$s/_patches" ]]; then
      require_cmd patch
      # Apply in sorted order (000-foo, 001-bar, ...) for determinism.
      for p in "$src/$s/_patches"/*.patch; do
        [[ -f "$p" ]] || continue
        log "      patch $(basename "$p")"
        ( cd "$work" && patch -p1 -s --no-backup-if-mismatch < "$p" )
      done
      rm -rf "$work/_patches"
    fi
    # tar-pipe over ssh in one round trip, then on the host:
    # (a) tar -xf with --no-same-owner so the extracted tree is owned
    #     by the sudo'd user (root) instead of whatever uid:gid the
    #     laptop checkout had — host-side ownership should not depend
    #     on the operator's account on a different machine;
    # (b) normalize Python shebangs so tools run regardless of where
    #     the sandbox image installs its interpreter;
    # (c) lock down modes: directories and *.py scripts to 0555
    #     (r-x), other files (requirements.txt, data) to 0444 (r--).
    #     Numeric modes, not `a=rX`, so scripts that aren't +x in the
    #     laptop checkout still end up executable on the host. The
    #     only intended way to mutate these files is a deliberate
    #     ops action (re-run with --force).
    tar -C "$work" -cf - . \
      | ssh_to "$h" "set -e
          sudo tar -C $remote_dir --no-same-owner -xf - --transform 's,^\.,$s,'
          sudo find $remote_dir/$s -type f -name '*.py' -exec \
            sed -i '1s|^#!.*python.*\$|#!/usr/bin/env python3|' {} +
          sudo find $remote_dir/$s -type d -exec chmod 0555 {} +
          sudo find $remote_dir/$s -type f -name '*.py' -exec chmod 0555 {} +
          sudo find $remote_dir/$s -type f ! -name '*.py' -exec chmod 0444 {} +"
    rm -rf "$work"
    trap - EXIT
  done
done

log "done."
