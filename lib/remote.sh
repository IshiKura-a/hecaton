# SSH wrapper. Authentication and connection details are delegated
# entirely to the user's local OpenSSH setup (~/.ssh/config + ssh-agent).
# Each inventory entry's `ssh_host` is treated as an ssh target as-is,
# so the recommended pattern is to put a `Host <ssh_host>` block in
# ~/.ssh/config with HostName / User / Port / IdentityFile.
#
#   ssh_to <host-name> <remote command...>
#   scp_to <host-name> <local-path> <remote-path>

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
# shellcheck source=inventory.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/inventory.sh"

_resolve_target() {
  local name="$1" host
  host="$(inventory_field "$name" ssh_host)"
  [[ -n "$host" ]] || die "inventory: host '$name' missing ssh_host"
  printf '%s\n' "$host"
}

ssh_to() {
  local name="$1"; shift
  local target
  target="$(_resolve_target "$name")"
  ssh -o ConnectTimeout=10 \
      -o StrictHostKeyChecking=accept-new \
      -o BatchMode=yes \
      "$target" "$@"
}

scp_to() {
  local name="$1" local_path="$2" remote_path="$3"
  local target
  target="$(_resolve_target "$name")"
  scp -o ConnectTimeout=10 \
      -o StrictHostKeyChecking=accept-new \
      -o BatchMode=yes \
      "$local_path" "$target:$remote_path"
}

# k8s node name = remote machine's hostname, lowercased. k3s registers
# nodes under that, so anything that needs to address a node via
# kubectl must translate inventory name -> k8s node name through here.
# Cached at $HECATON_ROOT/.cache/node-name/<host>; wipe to force re-probe.
node_name_for() {
  local host="$1"
  local cache="$HECATON_ROOT/.cache/node-name"
  mkdir -p "$cache"
  local cached="$cache/$host"
  if [[ -f "$cached" ]]; then
    cat "$cached"
    return
  fi
  local name
  name="$(ssh_to "$host" 'hostname' | tr '[:upper:]' '[:lower:]')"
  echo "$name" > "$cached"
  echo "$name"
}

# Run the same bash snippet (read from stdin) on each named host in
# parallel. Each host's combined stdout+stderr is buffered to a tempfile
# and replayed once all hosts finish. Exit code is non-zero if any host
# failed; the listing of failed hosts is printed via `warn`.
#
#   parallel_each_host "$env_prefix" "host1" "host2" ... <<< "$script"
#
# `env_prefix` is a string of `KEY=value` pairs (already quoted with
# `printf %q`) that get prepended to `bash -s` so the remote
# environment is populated. Pass an empty string to set no extra env.
parallel_each_host() {
  local env_prefix="$1"; shift
  local hosts=("$@")
  [[ ${#hosts[@]} -gt 0 ]] || die "parallel_each_host: no hosts"

  local script
  script="$(cat)"

  local tmpdir
  tmpdir="$(mktemp -d -t hecaton-parallel.XXXX)"

  local h pids=()
  for h in "${hosts[@]}"; do
    {
      ssh_to "$h" "$env_prefix bash -s" <<< "$script" \
        >"$tmpdir/$h.out" 2>&1
      echo $? > "$tmpdir/$h.rc"
    } &
    pids+=($!)
  done

  for pid in "${pids[@]}"; do
    wait "$pid" || true
  done

  local failed=()
  for h in "${hosts[@]}"; do
    log "==> $h"
    cat "$tmpdir/$h.out"
    local rc
    rc="$(cat "$tmpdir/$h.rc")"
    if [[ "$rc" != 0 ]]; then
      warn "==> $h: rc=$rc"
      failed+=("$h")
    fi
  done

  rm -rf "$tmpdir"

  if (( ${#failed[@]} > 0 )); then
    die "${#failed[@]} host(s) failed: ${failed[*]}"
  fi
}

# Warm a per-host fact by running the given function in parallel,
# discarding its output. Subsequent serial calls hit the on-disk
# cache and return instantly.
#
#   parallel_warm gpu_vendor "host1" "host2" ...
parallel_warm() {
  local fn="$1"; shift
  local h pids=()
  for h in "$@"; do
    "$fn" "$h" >/dev/null 2>&1 &
    pids+=($!)
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || true
  done
}
